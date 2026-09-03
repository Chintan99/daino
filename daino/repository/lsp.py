"""Language-server intelligence: diagnostics, definitions, references, symbols.

Daino bundles no language server, and deliberately so — shipping one per
language would mean vendoring hundreds of megabytes and taking ownership of
their release cycles. What it does instead is *use* the ones already on the
machine, which for anyone editing a project is nearly always the ones that
project already depends on.

The moving parts:

* :class:`LanguageServer` speaks LSP over a subprocess's stdin/stdout. That is
  the whole protocol surface: framed JSON-RPC, an initialize handshake, and
  request/notification plumbing. Diagnostics arrive unsolicited as
  ``textDocument/publishDiagnostics``, so they are collected as they land rather
  than polled for.
* :data:`SERVERS` describes the servers worth trying per language, in preference
  order, and how to invoke each. Detection is `shutil.which` plus the project's
  own ``.venv`` and ``node_modules/.bin`` — the same lookup order the QA service
  uses, for the same reason: a project's pinned tool beats a global one.
* :class:`LanguageServerPool` owns one server process per language, started on
  first use and reused after. Servers are expensive to start (pyright indexes
  the project) and cheap to keep, which is exactly the wrong shape for
  per-request spawning.

Everything here degrades rather than fails. A language with no server installed
reports "no server", not an error, and the editor is expected to say so — a
Problems panel that cannot tell "clean" from "nothing looked" is the bug this
module exists to avoid.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

from daino.schemas import RepositorySymbol

#: How long to wait for a server to answer one request. Generous, because the
#: first request after start can queue behind the server's initial index.
REQUEST_TIMEOUT_SECONDS = 20.0
#: How long to wait for the initialize handshake specifically.
INITIALIZE_TIMEOUT_SECONDS = 30.0
#: A file past this is not sent to a language server. They are interactive
#: tools; a generated megabyte bundle is not what they are for.
MAX_DOCUMENT_BYTES = 2_000_000


class LSPError(RuntimeError):
    """Raised when a language server cannot be started or stops responding."""


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """How to start one language server, and what it can be asked for."""

    #: Stable id used in API payloads, e.g. "pyright".
    id: str
    label: str
    #: Executable name looked up in the project, then on PATH.
    executable: str
    arguments: tuple[str, ...] = ()
    #: LSP language ids this server handles.
    languages: tuple[str, ...] = ()
    #: Install hint shown when it is missing, so "no diagnostics" is actionable.
    install: str = ""
    #: Some servers need a module invocation rather than a bare binary.
    python_module: str = ""


#: Language id -> servers to try, best first.
#:
#: Kept small on purpose. Each entry is a server that (a) speaks stdio LSP with
#: no configuration, and (b) is something a project in that language plausibly
#: already has. Adding one is a data change, not a code change.
SERVERS: dict[str, tuple[ServerSpec, ...]] = {
    "python": (
        ServerSpec(
            id="pyright",
            label="Pyright",
            executable="pyright-langserver",
            arguments=("--stdio",),
            languages=("python",),
            install="npm i -g pyright  (or: pip install pyright)",
        ),
        ServerSpec(
            id="pylsp",
            label="Python LSP Server",
            executable="pylsp",
            languages=("python",),
            install="pip install 'python-lsp-server[all]'",
            python_module="pylsp",
        ),
        ServerSpec(
            id="jedi-language-server",
            label="Jedi Language Server",
            executable="jedi-language-server",
            languages=("python",),
            install="pip install jedi-language-server",
        ),
    ),
    "typescript": (
        ServerSpec(
            id="typescript-language-server",
            label="TypeScript",
            executable="typescript-language-server",
            arguments=("--stdio",),
            languages=("typescript", "typescriptreact", "javascript", "javascriptreact"),
            install="npm i -g typescript typescript-language-server",
        ),
    ),
    "go": (
        ServerSpec(
            id="gopls",
            label="gopls",
            executable="gopls",
            languages=("go",),
            install="go install golang.org/x/tools/gopls@latest",
        ),
    ),
    "rust": (
        ServerSpec(
            id="rust-analyzer",
            label="rust-analyzer",
            executable="rust-analyzer",
            languages=("rust",),
            install="rustup component add rust-analyzer",
        ),
    ),
}

# typescript's server covers four language ids; register the aliases so a .tsx
# file finds it without every caller having to know that.
for _alias in ("typescriptreact", "javascript", "javascriptreact"):
    SERVERS[_alias] = SERVERS["typescript"]

#: File suffix -> LSP language id. Only languages a server above can serve.
LANGUAGE_IDS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
}


def language_id_for(path: Path | str) -> str:
    """The LSP language id for a path, or "" when no server could serve it."""
    return LANGUAGE_IDS.get(Path(path).suffix.lower(), "")


def to_uri(path: Path) -> str:
    return "file://" + pathname2url(str(path.resolve()))


def from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


# --------------------------------------------------------------- detection


def resolve_executable(root: Path, spec: ServerSpec) -> list[str] | None:
    """The argv to start ``spec`` with, or None when it is not installed.

    A project's own pinned server wins over a global one — that is the whole
    reason the local paths are checked first. Same order as the QA service uses
    for linters, so "which tool ran" has one answer across the product.
    """
    candidates = [
        root / "node_modules" / ".bin" / spec.executable,
        root / ".venv" / "bin" / spec.executable,
        root / ".venv" / "Scripts" / f"{spec.executable}.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate), *spec.arguments]
    found = shutil.which(spec.executable)
    if found:
        return [found, *spec.arguments]
    if spec.python_module:
        # A server installed as a library rather than a script still works when
        # invoked as a module, which is common inside a project virtualenv.
        for interpreter in (root / ".venv" / "bin" / "python", Path(sys.executable)):
            if not interpreter.is_file():
                continue
            probe = [str(interpreter), "-c", f"import {spec.python_module}"]
            with contextlib.suppress(OSError):
                import subprocess  # noqa: PLC0415 - probe only, never a shell

                if subprocess.run(probe, capture_output=True, check=False).returncode == 0:  # noqa: S603
                    return [str(interpreter), "-m", spec.python_module, *spec.arguments]
    return None


def available_servers(root: Path) -> list[dict[str, Any]]:
    """Every known server, whether it is installed here, and how to get it.

    Reported to the GUI so a language with no diagnostics says *why* and what to
    install, rather than looking identical to a language with no problems.
    """
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for language, specs in SERVERS.items():
        for spec in specs:
            if spec.id in seen:
                continue
            seen.add(spec.id)
            rows.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "languages": list(spec.languages) or [language],
                    "available": resolve_executable(root, spec) is not None,
                    "install": spec.install,
                }
            )
    rows.sort(key=lambda row: (not row["available"], row["label"]))
    return rows


# ------------------------------------------------------------- the protocol


@dataclass
class _Pending:
    future: asyncio.Future[Any]


class LanguageServer:
    """One language-server process, spoken to over stdio.

    Deliberately thin: it frames JSON-RPC, tracks in-flight requests by id, and
    files unsolicited notifications. Everything language-specific lives in the
    server itself, which is the point of using a protocol rather than writing an
    analyser per language.
    """

    def __init__(self, root: Path, spec: ServerSpec, argv: list[str]) -> None:
        self.root = root.resolve()
        self.spec = spec
        self.argv = argv
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        #: uri -> the diagnostics the server last published for it.
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        #: uri -> version, so a reopened document is not announced twice.
        self._open: dict[str, int] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        #: Last lines of stderr, kept so a crash can say what the server said.
        self._errors: list[str] = []
        self._ready = asyncio.Event()
        self._closed = False
        #: Set when a document's diagnostics arrive, so a caller can wait for
        #: the first publish rather than guessing at a sleep.
        self._published: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Spawn the server and complete the initialize handshake."""
        if self.process is not None:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.root),
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, ValueError) as exc:
            raise LSPError(f"{self.spec.label} could not be started: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._initialize(), INITIALIZE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            await self.close()
            raise LSPError(
                f"{self.spec.label} did not finish starting up within "
                f"{INITIALIZE_TIMEOUT_SECONDS:.0f}s."
            ) from exc
        self._ready.set()

    async def _initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": to_uri(self.root),
                "rootPath": str(self.root),
                "workspaceFolders": [
                    {"uri": to_uri(self.root), "name": self.root.name},
                ],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": True, "dynamicRegistration": False},
                        "publishDiagnostics": {"relatedInformation": True},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "implementation": {"linkSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                    },
                    "workspace": {
                        "workspaceFolders": True,
                        "symbol": {},
                        "configuration": True,
                    },
                },
                # Pyright reads these; other servers ignore what they do not know.
                "initializationOptions": {},
            },
        )
        self.notify("initialized", {})
        # Servers that ask for configuration get an empty-but-valid answer, which
        # is enough for the defaults every one of these ships with.
        self.notify("workspace/didChangeConfiguration", {"settings": {}})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        self.process = None
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(LSPError("The language server stopped."))
        self._pending.clear()
        if process is None:
            return
        with contextlib.suppress(Exception):
            process.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), 3.0)
        if process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    # -------------------------------------------------------------- plumbing

    def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise LSPError(f"{self.spec.label} is not running.")
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        process.stdin.write(header + body)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        identifier = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[identifier] = _Pending(future)
        self._write({"id": identifier, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, REQUEST_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self._pending.pop(identifier, None)
            raise LSPError(
                f"{self.spec.label} did not answer {method} within {REQUEST_TIMEOUT_SECONDS:.0f}s."
            ) from exc

    async def _read_loop(self) -> None:
        """Read framed messages until the server's stdout closes."""
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        try:
            while True:
                length = 0
                # Headers, terminated by a blank line. Only Content-Length is
                # load-bearing; Content-Type is allowed and ignored.
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        break
                    name, _, value = text.partition(":")
                    if name.strip().casefold() == "content-length":
                        with contextlib.suppress(ValueError):
                            length = int(value.strip())
                if length <= 0:
                    continue
                payload = await stream.readexactly(length)
                try:
                    message = json.loads(payload.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            return
        except Exception:  # noqa: BLE001 - a dead reader must not kill the app
            return

    def _dispatch(self, message: dict[str, Any]) -> None:
        identifier = message.get("id")
        if "method" in message and identifier is None:
            self._handle_notification(message)
            return
        if "method" in message and identifier is not None:
            # A server-to-client request. Answering "null" keeps servers that
            # ask for configuration or registration from stalling.
            self._answer_server_request(message)
            return
        pending = self._pending.pop(identifier, None) if identifier is not None else None
        if pending is None or pending.future.done():
            return
        if "error" in message:
            detail = message["error"]
            reason = detail.get("message") or detail if isinstance(detail, dict) else detail
            pending.future.set_exception(LSPError(str(reason)))
            return
        pending.future.set_result(message.get("result"))

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        if method == "workspace/configuration":
            items = message.get("params", {}).get("items") or [{}]
            result: Any = [{} for _ in items]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": to_uri(self.root), "name": self.root.name}]
        else:
            result = None
        with contextlib.suppress(LSPError):
            self._write({"id": message.get("id"), "result": result})

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if method == "textDocument/publishDiagnostics":
            uri = str(params.get("uri", ""))
            self.diagnostics[uri] = list(params.get("diagnostics") or [])
            event = self._published.get(uri)
            if event is not None:
                event.set()

    # ------------------------------------------------------------ documents

    async def open_document(self, path: Path, text: str | None = None) -> str:
        """Announce a document, or update the copy the server already has.

        Sending the editor's text rather than letting the server read from disk
        is what makes diagnostics describe the buffer the user is looking at.
        """
        uri = to_uri(path)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise LSPError(f"{path.name} could not be read as text.") from exc
        if len(text.encode("utf-8", "replace")) > MAX_DOCUMENT_BYTES:
            raise LSPError(f"{path.name} is too large for a language server.")
        language = language_id_for(path)
        self._published.setdefault(uri, asyncio.Event()).clear()
        if uri in self._open:
            version = self._open[uri] + 1
            self._open[uri] = version
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
            return uri
        self._open[uri] = 1
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language,
                    "version": 1,
                    "text": text,
                }
            },
        )
        return uri

    def close_document(self, path: Path) -> None:
        uri = to_uri(path)
        if self._open.pop(uri, None) is None:
            return
        self.diagnostics.pop(uri, None)
        self._published.pop(uri, None)
        with contextlib.suppress(LSPError):
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    async def wait_for_diagnostics(self, uri: str, timeout: float = 6.0) -> list[dict[str, Any]]:
        """Diagnostics for a document, waiting for the first publish.

        A server publishes when it has finished analysing, not when asked, so a
        caller that returns immediately reports "no problems" for every file it
        has only just opened. Timing out returns whatever has landed — possibly
        an empty list, which for a clean file is the correct answer.
        """
        event = self._published.setdefault(uri, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
        return list(self.diagnostics.get(uri, []))

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                self._errors.append(line.decode("utf-8", "replace").rstrip())
                del self._errors[:-40]

    @property
    def last_errors(self) -> list[str]:
        return list(self._errors)


# ------------------------------------------------------------------- pool


@dataclass
class _Slot:
    server: LanguageServer | None = None
    #: Why this language has no server, when it has none.
    reason: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LanguageServerPool:
    """One server process per language, started on demand and then reused.

    Starting a server is expensive (pyright indexes the project before it
    answers anything) and keeping one is cheap, so per-request spawning would be
    the one shape guaranteed to feel broken. The pool also means a failure is
    remembered: a language whose server is missing is not re-probed on every
    keystroke.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._slots: dict[str, _Slot] = {}

    def _slot(self, language: str) -> _Slot:
        return self._slots.setdefault(language, _Slot())

    async def server_for(self, language: str) -> LanguageServer:
        """The running server for ``language``, starting one if needed."""
        specs = SERVERS.get(language)
        if not specs:
            raise LSPError(f"No language server is known for {language or 'this file type'}.")
        slot = self._slot(language)
        async with slot.lock:
            if slot.server is not None and slot.server.alive:
                return slot.server
            if slot.server is not None:
                # It died. Forget it and try again — a crashed server should not
                # make the feature permanently unavailable.
                slot.server = None
            attempted: list[str] = []
            for spec in specs:
                argv = resolve_executable(self.root, spec)
                if argv is None:
                    attempted.append(f"{spec.label} (install: {spec.install})")
                    continue
                server = LanguageServer(self.root, spec, argv)
                try:
                    await server.start()
                except LSPError as exc:
                    attempted.append(f"{spec.label} ({exc})")
                    await server.close()
                    continue
                slot.server = server
                slot.reason = ""
                return server
            slot.reason = (
                "No language server available for "
                + language
                + ". Tried: "
                + ("; ".join(attempted) or "nothing")
            )
            raise LSPError(slot.reason)

    async def server_for_path(self, path: Path) -> LanguageServer:
        language = language_id_for(path)
        if not language:
            raise LSPError(f"{path.name} has no language server.")
        return await self.server_for(language)

    async def close(self) -> None:
        for slot in self._slots.values():
            if slot.server is not None:
                await slot.server.close()
                slot.server = None

    def running(self) -> list[dict[str, Any]]:
        """Which servers are up, for the status surface."""
        return [
            {
                "language": language,
                "server": slot.server.spec.id,
                "label": slot.server.spec.label,
            }
            for language, slot in self._slots.items()
            if slot.server is not None and slot.server.alive
        ]


# ------------------------------------------------------- the adapter contract


class LSPAdapter(ABC):
    """The semantic-query surface, kept abstract so the pool is swappable."""

    @abstractmethod
    async def start(self, root: Path) -> None: ...

    @abstractmethod
    async def symbols(self, path: Path) -> list[RepositorySymbol]: ...

    @abstractmethod
    async def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def close(self) -> None: ...


#: LSP SymbolKind -> a name a person reads. Index is the protocol's own value.
SYMBOL_KINDS: dict[int, str] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum-member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type-parameter",
}

#: LSP DiagnosticSeverity -> the editor's own vocabulary.
SEVERITIES: dict[int, str] = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class PooledLSPAdapter(LSPAdapter):
    """The real adapter: whatever language servers this machine has.

    Positions are LSP's own — zero-based line and character. Callers that speak
    editor coordinates (one-based) convert at the boundary rather than here, so
    there is exactly one place in the codebase where the two disagree.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.pool = LanguageServerPool(root or Path.cwd())

    async def start(self, root: Path) -> None:
        self.pool = LanguageServerPool(root)

    async def close(self) -> None:
        await self.pool.close()

    # ---------------------------------------------------------- diagnostics

    async def diagnostics(
        self, path: Path, text: str | None = None, *, timeout: float = 6.0
    ) -> list[dict[str, Any]]:
        """Problems in one document, as the language server sees it."""
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path, text)
        raw = await server.wait_for_diagnostics(uri, timeout)
        return [_diagnostic(path, item) for item in raw]

    def close_document(self, path: Path) -> None:
        """Tell the server a file is no longer open, if one is running for it."""
        language = language_id_for(path)
        slot = self.pool._slots.get(language) if language else None
        if slot is not None and slot.server is not None:
            slot.server.close_document(path)

    # ------------------------------------------------------------- symbols

    async def symbols(self, path: Path) -> list[RepositorySymbol]:
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path)
        result = await server.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        relative = _relative(path, self.pool.root)
        found: list[RepositorySymbol] = []
        _flatten_symbols(result or [], relative, found)
        return found

    async def workspace_symbols(self, query: str, language: str = "") -> list[RepositorySymbol]:
        """Symbols across the project, from every server that can answer.

        Asking all running servers rather than one is what makes a single search
        box work in a repository with a Python backend and a TypeScript
        frontend, which is most of them.

        Raises :class:`LSPError` when *no* server could be reached, so a caller
        can tell "searched and found nothing" from "nothing searched" — the same
        distinction the diagnostics path turns on.
        """
        languages = [language] if language else sorted({*self.pool._slots, *SERVERS})
        found: list[RepositorySymbol] = []
        seen: set[tuple[str, str, int]] = set()
        answered = False
        for item in languages:
            if item not in SERVERS:
                continue
            try:
                server = await self.pool.server_for(item)
                result = await server.request("workspace/symbol", {"query": query})
            except LSPError:
                continue
            answered = True
            for entry in result or []:
                symbol = _workspace_symbol(entry, self.pool.root)
                if symbol is None:
                    continue
                key = (symbol.name, symbol.path, symbol.line)
                if key in seen:
                    continue
                seen.add(key)
                found.append(symbol)
        if not answered:
            # An empty list from a server that ran means "no such symbol"; an
            # empty list because nothing ran means something else entirely, and
            # a caller that cannot tell them apart will label index results as
            # language-server results.
            raise LSPError("No language server answered a workspace symbol query.")
        found.sort(key=lambda symbol: (symbol.name.casefold(), symbol.path, symbol.line))
        return found

    # ---------------------------------------------------------- navigation

    async def definition(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        return await self._locations("textDocument/definition", path, line, column)

    async def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path)
        result = await server.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "context": {"includeDeclaration": True},
            },
        )
        return [_location(item, self.pool.root) for item in _as_locations(result)]

    async def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        return await self._locations("textDocument/implementation", path, line, column)

    async def hover(self, path: Path, line: int, column: int) -> str:
        """The one-panel explanation of a symbol, as markdown."""
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path)
        result = await server.request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        return _hover_text(result)

    async def rename_edits(
        self, path: Path, line: int, column: int, new_name: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Every edit a rename implies, keyed by repository-relative path.

        Returned rather than applied: a cross-file refactor is exactly the kind
        of change that should be shown before it happens.
        """
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path)
        result = await server.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "newName": new_name,
            },
        )
        return _workspace_edit(result, self.pool.root)

    async def _locations(
        self, method: str, path: Path, line: int, column: int
    ) -> list[dict[str, Any]]:
        server = await self.pool.server_for_path(path)
        uri = await server.open_document(path)
        result = await server.request(
            method,
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        return [_location(item, self.pool.root) for item in _as_locations(result)]


# --------------------------------------------------------------- conversion


def _relative(path: Path, root: Path) -> str:
    with contextlib.suppress(ValueError):
        return path.resolve().relative_to(root).as_posix()
    return path.as_posix()


def _diagnostic(path: Path, item: dict[str, Any]) -> dict[str, Any]:
    start = (item.get("range") or {}).get("start") or {}
    end = (item.get("range") or {}).get("end") or {}
    return {
        "path": path.as_posix(),
        # One-based, because every editor and every error message is.
        "line": int(start.get("line", 0)) + 1,
        "column": int(start.get("character", 0)) + 1,
        "end_line": int(end.get("line", start.get("line", 0))) + 1,
        "end_column": int(end.get("character", start.get("character", 0))) + 1,
        "severity": SEVERITIES.get(int(item.get("severity") or 1), "info"),
        "message": str(item.get("message", "")),
        "source": str(item.get("source") or ""),
        "code": str(item.get("code") or ""),
    }


def _as_locations(result: Any) -> list[dict[str, Any]]:
    """Normalise the three shapes LSP allows for a location answer."""
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    return [item for item in result if isinstance(item, dict)]


def _location(item: dict[str, Any], root: Path) -> dict[str, Any]:
    # A LocationLink uses targetUri/targetRange; a Location uses uri/range.
    uri = str(item.get("uri") or item.get("targetUri") or "")
    span = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange") or {}
    start = span.get("start") or {}
    path = from_uri(uri) if uri else Path()
    return {
        "path": _relative(path, root),
        "line": int(start.get("line", 0)) + 1,
        "column": int(start.get("character", 0)) + 1,
    }


def _flatten_symbols(items: Any, relative: str, into: list[RepositorySymbol]) -> None:
    """Collect DocumentSymbol trees and SymbolInformation lists alike."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        span = item.get("selectionRange") or item.get("range") or {}
        if not span and isinstance(item.get("location"), dict):
            span = item["location"].get("range") or {}
        line = int((span.get("start") or {}).get("line", 0)) + 1
        into.append(
            RepositorySymbol(
                name=str(item.get("name", "")),
                kind=SYMBOL_KINDS.get(int(item.get("kind") or 0), "symbol"),
                path=relative,
                line=line,
                signature=str(item.get("detail")) if item.get("detail") else None,
            )
        )
        _flatten_symbols(item.get("children"), relative, into)


def _workspace_symbol(entry: dict[str, Any], root: Path) -> RepositorySymbol | None:
    location = entry.get("location")
    if not isinstance(location, dict):
        return None
    uri = str(location.get("uri", ""))
    if not uri:
        return None
    start = (location.get("range") or {}).get("start") or {}
    return RepositorySymbol(
        name=str(entry.get("name", "")),
        kind=SYMBOL_KINDS.get(int(entry.get("kind") or 0), "symbol"),
        path=_relative(from_uri(uri), root),
        line=int(start.get("line", 0)) + 1,
        signature=str(entry.get("containerName")) if entry.get("containerName") else None,
    )


def _hover_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    if isinstance(contents, list):
        parts = [
            item if isinstance(item, str) else str(item.get("value", ""))
            for item in contents
            if isinstance(item, (str, dict))
        ]
        return "\n\n".join(part for part in parts if part)
    return ""


def _workspace_edit(result: Any, root: Path) -> dict[str, list[dict[str, Any]]]:
    """Flatten a WorkspaceEdit into per-path edit lists, in editor coordinates."""
    if not isinstance(result, dict):
        return {}
    edits: dict[str, list[dict[str, Any]]] = {}

    def add(uri: str, items: Any) -> None:
        path = _relative(from_uri(uri), root)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            span = item.get("range") or {}
            start = span.get("start") or {}
            end = span.get("end") or {}
            edits.setdefault(path, []).append(
                {
                    "start_line": int(start.get("line", 0)) + 1,
                    "start_column": int(start.get("character", 0)) + 1,
                    "end_line": int(end.get("line", 0)) + 1,
                    "end_column": int(end.get("character", 0)) + 1,
                    "text": str(item.get("newText", "")),
                }
            )

    for uri, items in (result.get("changes") or {}).items():
        add(str(uri), items)
    for change in result.get("documentChanges") or []:
        if not isinstance(change, dict):
            continue
        document = change.get("textDocument") or {}
        if isinstance(document, dict) and document.get("uri"):
            add(str(document["uri"]), change.get("edits"))
    return edits
