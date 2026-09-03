"""Structured JSON audit logging with mandatory redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daino.config import paths
from daino.security.secrets import redact

logger = logging.getLogger(__name__)


class AuditLog:
    """Append-only project audit log."""

    def __init__(self, root: Path) -> None:
        self.path = paths.state_dir(root) / "logs" / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        """Append one redacted event as a single line of JSON.

        Redaction runs over the payload's *values*, never over the serialized
        line. Redacting the line was the original approach and it corrupted the
        log: ``redact`` rewrites a matched span to ``[REDACTED]``, so a match
        that straddled JSON escaping — a diff containing ``api_key = str(...)``
        was enough — swallowed an escaped quote and left a bare one behind. The
        line stopped parsing, and one such line used to take the whole log with
        it. Redacting first and serializing after makes that structurally
        impossible: whatever the redactor produces, the encoder escapes it.
        """
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        # Round-tripped through the encoder first so anything exotic is already
        # the string it will be written as — otherwise a value that only
        # becomes text via ``default=str`` would reach the file unredacted.
        normalized = json.loads(json.dumps(payload, default=str, ensure_ascii=False))
        safe = json.dumps(_redacted(normalized), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(safe + "\n")

    def read(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """Every event in the log, skipping any line that will not parse.

        A single unreadable line must not cost the reader the other thousands:
        this is an append-only file written by many processes over months, and
        a truncated final write or a line from an older, buggier build is a
        thing that happens. Skips are counted and logged rather than hidden.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        skipped = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(item, dict):
                skipped += 1
                continue
            if mission_id is None or item.get("mission_id") == mission_id:
                events.append(item)
        if skipped:
            logger.warning("Skipped %d unreadable line(s) in %s", skipped, self.path)
        return events


def _redacted(value: Any) -> Any:
    """Redact every string inside a JSON-shaped value, structure untouched.

    Keys are left alone deliberately: they are Daino's own field names, and a
    redacted key would break the shape every reader depends on.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def configure_logging(level: str = "INFO") -> None:
    """Set the root log level, whether or not logging is already configured.

    ``basicConfig`` is a no-op once the root logger has handlers — which it does
    as soon as uvicorn or a prior call installed them — so a later change of
    level (from the GUI's Settings menu, for instance) would silently do nothing
    without the explicit ``setLevel``.
    """
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(level)
