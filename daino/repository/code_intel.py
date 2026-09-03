"""Language-server answers, shaped for an agent rather than an editor.

``daino.repository.lsp`` is a complete LSP client, and everything it knew went to
the IDE. The agent edited blind: it could rename a function and learn nothing
about the four callers it had just broken until, several steps later, it thought
to run the tests. A compiler had that answer immediately and nobody asked it.

Two things are needed to close that, and they are different in kind:

* **Diagnostics after an edit**, unasked. This is the one that matters. The agent
  did not know to ask, which is precisely why it has to be told.
* **Definition and reference lookup**, on request. The agent knows when it wants
  these; it simply had no way to say so.

The interface is deliberately not the LSP one. A language server speaks in
zero-based line and character offsets, which a model has to derive from a file it
read as text — arithmetic it gets wrong often enough to make the tool a net
negative. Here it names a *symbol*, and the position is resolved from the source.

Everything degrades to silence. No server installed, a server that will not
start, a language nobody wrote a server for, a request that times out: all of
these produce no diagnostics rather than an error. An agent must never be blocked
from editing a file because a language server is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daino.repository.lsp import (
    SERVERS,
    LSPError,
    PooledLSPAdapter,
    language_id_for,
    resolve_executable,
)

LOGGER = logging.getLogger(__name__)

#: How long an unasked-for post-edit diagnostic pass may take. Short: this runs
#: after every write, and an agent waiting eight seconds per edit for a first
#: opinion is worse off than one that runs the tests at the end.
DIAGNOSTICS_TIMEOUT_SECONDS = 6.0

#: Longer, because the agent asked and is waiting on the answer.
REQUEST_TIMEOUT_SECONDS = 15.0

#: Diagnostics reported in one observation. A file with two hundred pre-existing
#: warnings would otherwise bury the one thing the edit just broke.
MAX_DIAGNOSTICS = 20

#: Severities worth interrupting the agent with after an edit. Hints and
#: information are the noise that makes a feed like this get ignored; a real
#: error or warning is the signal it exists for.
REPORTED_SEVERITIES = frozenset({"error", "warning"})


@dataclass
class CodeIntelligence:
    """The agent's view of what a language server knows."""

    root: Path
    #: Substituted in tests. Production constructs a pooled adapter lazily, so a
    #: session that never edits a supported file never starts a server.
    adapter: Any = None
    enabled: bool = True
    diagnostics_timeout: float = DIAGNOSTICS_TIMEOUT_SECONDS
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    #: Language -> whether a server for it is installed here. Resolving an
    #: executable touches the filesystem and PATH; after an edit is the wrong
    #: place to do that repeatedly.
    _supported: dict[str, bool] = field(default_factory=dict, repr=False)
    #: Set once a language server has failed badly enough that retrying it every
    #: edit would just be a slow way of getting the same nothing.
    _broken: set[str] = field(default_factory=set, repr=False)

    def supports(self, relative: str | Path) -> bool:
        """Whether a server for this file's language is installed on this machine.

        Answered without starting anything, because the caller is deciding
        whether it is worth trying at all.
        """
        if not self.enabled:
            return False
        language = language_id_for(Path(relative))
        if not language or language in self._broken:
            return False
        cached = self._supported.get(language)
        if cached is None:
            cached = any(
                resolve_executable(self.root, spec) is not None
                for spec in SERVERS.get(language, ())
            )
            self._supported[language] = cached
        return cached

    async def diagnostics(self, relative: str | Path) -> list[dict[str, Any]]:
        """Current problems in one file, or an empty list for any reason at all."""
        rows = await self._call("diagnostics", relative, timeout=self.diagnostics_timeout)
        return rows or []

    async def definition(self, relative: str | Path, symbol: str) -> dict[str, Any]:
        """Where a symbol is defined, plus whatever the server can say about it."""
        located = self._locate(relative, symbol)
        if located is None:
            return {"error": _not_found(relative, symbol)}
        line, column = located
        adapter = await self._adapter()
        if adapter is None:
            return {"error": _unavailable(relative)}
        try:
            async with asyncio.timeout(self.request_timeout):
                locations = await adapter.definition(Path(self.root, relative), line, column)
                summary = await adapter.hover(Path(self.root, relative), line, column)
        except (LSPError, TimeoutError, OSError) as exc:
            return {"error": f"The language server could not answer: {exc}"}
        return {"symbol": symbol, "locations": locations, "summary": summary}

    async def references(self, relative: str | Path, symbol: str) -> dict[str, Any]:
        """Every place a symbol is used, which is what a rename actually needs."""
        located = self._locate(relative, symbol)
        if located is None:
            return {"error": _not_found(relative, symbol)}
        line, column = located
        adapter = await self._adapter()
        if adapter is None:
            return {"error": _unavailable(relative)}
        try:
            async with asyncio.timeout(self.request_timeout):
                locations = await adapter.references(Path(self.root, relative), line, column)
        except (LSPError, TimeoutError, OSError) as exc:
            return {"error": f"The language server could not answer: {exc}"}
        return {"symbol": symbol, "locations": locations}

    def _locate(self, relative: str | Path, symbol: str) -> tuple[int, int] | None:
        """Zero-based (line, column) of ``symbol`` in the file, or ``None``.

        Whole-word, first occurrence. A definition is almost always the first
        mention in its own file, and when it is not, the server's
        ``textDocument/definition`` resolves from a use to the definition anyway —
        so the first hit is the right guess in both cases.
        """
        if not symbol.strip():
            return None
        path = Path(self.root, relative)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        pattern = re.compile(rf"\b{re.escape(symbol.strip())}\b")
        for index, line in enumerate(text.splitlines()):
            match = pattern.search(line)
            if match is not None:
                return (index, match.start())
        return None

    async def _adapter(self) -> Any:
        if not self.enabled:
            return None
        if self.adapter is None:
            adapter = PooledLSPAdapter(self.root)
            await adapter.start(self.root)
            self.adapter = adapter
        return self.adapter

    async def _call(
        self, method: str, relative: str | Path, *, timeout: float
    ) -> list[dict[str, Any]] | None:
        if not self.supports(relative):
            return None
        adapter = await self._adapter()
        if adapter is None:
            return None
        language = language_id_for(Path(relative))
        try:
            async with asyncio.timeout(timeout):
                rows = await getattr(adapter, method)(Path(self.root, relative))
                return list(rows or [])
        except TimeoutError:
            # Not fatal on its own — a cold server's first request is slow — so
            # the language is not marked broken for one timeout.
            LOGGER.debug("LSP %s timed out for %s", method, relative)
            return None
        except (LSPError, OSError) as exc:
            LOGGER.debug("LSP %s failed for %s: %s", relative, method, exc)
            if language:
                # A server that errors rather than merely being slow will keep
                # erroring, and retrying it after every edit is a slow way of
                # getting the same nothing.
                self._broken.add(language)
            return None

    async def aclose(self) -> None:
        adapter = self.adapter
        self.adapter = None
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.close()


def edit_feedback(relative: str, rows: list[dict[str, Any]]) -> str:
    """What to tell the agent about a file it just wrote. ``""`` when it is clean.

    Filtered to errors and warnings, and capped. This text is appended to every
    successful edit's observation, so its cost is paid on every step of every
    turn — it has to earn its place each time or the agent learns to skim past it.
    """
    reported = [row for row in rows if row.get("severity") in REPORTED_SEVERITIES]
    if not reported:
        return ""
    reported.sort(key=lambda row: (row.get("severity") != "error", row.get("line", 0)))
    shown = reported[:MAX_DIAGNOSTICS]
    lines = [
        f"{relative}:{row.get('line')}:{row.get('column')} "
        f"{row.get('severity')}: {row.get('message')}"
        + (f" [{row['source']}]" if row.get("source") else "")
        for row in shown
    ]
    omitted = len(reported) - len(shown)
    if omitted:
        lines.append(f"… and {omitted} more")
    errors = sum(1 for row in reported if row.get("severity") == "error")
    header = (
        f"LANGUAGE SERVER — {errors} error(s) and {len(reported) - errors} warning(s) in this "
        "file after your edit. Some may predate it; fix what your change caused before "
        "moving on."
    )
    return header + "\n" + "\n".join(lines)


def render_locations(payload: dict[str, Any], *, label: str) -> str:
    """One reference or definition list, as lines the agent can act on."""
    if payload.get("error"):
        return str(payload["error"])
    locations = payload.get("locations") or []
    symbol = payload.get("symbol", "")
    if not locations:
        return f"No {label} found for {symbol}."
    lines = [
        f"- {item.get('path')}:{item.get('line')}:{item.get('column')}"
        for item in locations
        if isinstance(item, dict)
    ]
    summary = str(payload.get("summary") or "").strip()
    body = f"{label.capitalize()} of {symbol}:\n" + "\n".join(lines)
    return f"{body}\n\n{summary}" if summary else body


def _not_found(relative: str | Path, symbol: str) -> str:
    return (
        f"{symbol!r} does not appear in {relative}. Check the spelling, or read the file "
        "to find the name actually used there."
    )


def _unavailable(relative: str | Path) -> str:
    return (
        f"No language server is available for {relative} on this machine, so definitions and "
        "references cannot be resolved. Use grep instead."
    )
