"""The audit log: redacted on write, and readable even when a line is not.

Both properties were broken together in one incident. Redaction ran over the
serialized line, so a diff containing ``api_key = str(...)`` rewrote a span that
straddled JSON escaping and left a bare quote behind; the line stopped parsing;
and because the reader raised on the first bad line, one corrupt entry took
2,000 good ones with it. These tests pin the pair.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from daino.observability import AuditLog

#: The line from a real project's log that corrupted it. A source file assigning
#: ``api_key`` is enough — the value is not a secret at all, which is precisely
#: why nobody expected redaction to touch it.
POISON_DIFF = '326 +         api_key = str(data.get("api_key", "")).strip()'


def test_a_redacted_value_never_breaks_the_line_around_it(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)

    log.emit("event.FileChanged", mission_id="m-1", payload={"diff": POISON_DIFF})

    line = log.path.read_text(encoding="utf-8").splitlines()[0]
    event = json.loads(line)  # the assertion: it parses at all
    assert event["mission_id"] == "m-1"
    assert "[REDACTED]" in event["payload"]["diff"]


def test_secrets_are_removed_wherever_they_are_nested(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)

    log.emit(
        "event.ToolCompleted",
        payload={
            "output": "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345",
            "nested": {"items": ["Authorization: Bearer sk-zyxwvutsrqponmlkjihgfedcba"]},
        },
    )

    written = log.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in written
    assert "sk-zyxwvutsrqponmlkjihgfedcba" not in written
    assert written.count("[REDACTED]") >= 2


def test_a_value_that_only_becomes_text_when_serialized_is_still_redacted(
    tmp_path: Path,
) -> None:
    """Nothing may reach the file by a route that skips the redactor."""

    class Opaque:
        def __str__(self) -> str:
            return "token=sk-abcdefghijklmnopqrstuvwxyz012345"

    log = AuditLog(tmp_path)

    log.emit("event.Custom", payload={"detail": Opaque()})

    written = log.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in written
    assert "[REDACTED]" in written


def test_one_unreadable_line_does_not_cost_the_reader_the_others(
    tmp_path: Path, caplog: object
) -> None:
    """An append-only file written over months will eventually hold a bad line."""
    log = AuditLog(tmp_path)
    log.emit("event.One", mission_id="m-1")
    log.emit("event.Two", mission_id="m-2")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"timestamp": "truncated", "event": "event.Thr\n')
    log.emit("event.Four", mission_id="m-1")

    with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
        events = log.read()

    assert [item["event"] for item in events] == ["event.One", "event.Two", "event.Four"]
    # Skipped, not hidden.
    assert "Skipped 1 unreadable line" in caplog.text  # type: ignore[attr-defined]


def test_filtering_by_mission_survives_a_corrupt_neighbour(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.emit("event.One", mission_id="m-1")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write("[1, 2, 3]\n")  # valid JSON, wrong shape
    log.emit("event.Two", mission_id="m-1")

    assert [item["event"] for item in log.read("m-1")] == ["event.One", "event.Two"]


def test_a_missing_log_reads_as_empty(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.path.unlink(missing_ok=True)

    assert log.read() == []
