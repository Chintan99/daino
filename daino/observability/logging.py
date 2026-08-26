"""Structured JSON audit logging with mandatory redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daino.config import paths
from daino.security.secrets import redact


class AuditLog:
    """Append-only project audit log."""

    def __init__(self, root: Path) -> None:
        self.path = paths.state_dir(root) / "logs" / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        safe = redact(json.dumps(payload, default=str, ensure_ascii=False))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(safe + "\n")

    def read(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if mission_id is None or item.get("mission_id") == mission_id:
                events.append(item)
        return events


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
