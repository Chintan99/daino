from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daino.application import QAApplicationService, initialize_project, open_project
from daino.schemas import QAReport


@pytest.mark.asyncio
async def test_qa_service_runs_available_checks_skips_network_and_persists(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'qa-sample'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    service = QAApplicationService(context)
    approvals: list[tuple[str, str]] = []

    async def decline(subject: str, reason: str) -> tuple[bool, bool]:
        approvals.append((subject, reason))
        return False, False

    try:
        report = await service.run(approve=decline)

        assert report.status == "completed"
        assert next(item for item in report.checks if item.id == "python-syntax").status == "passed"
        audit = next(item for item in report.checks if item.id == "python-audit")
        assert audit.status == "skipped"
        if audit.command:
            assert approvals and "network" in approvals[0][1]
        assert all(item.status == "skipped" for item in report.specialists)
        assert report.project_root == str(tmp_path.resolve())
        assert service.latest() == report
        assert service.history() == [report]
        assert service.load(report.id) == report
        assert service.load("../outside") is None
        assert (tmp_path / ".daino" / "qa" / f"{report.id}.json").exists()
        details = service.missions.mission_details(report.mission_id)
        assert details["mission"]["status"] == "completed"
    finally:
        context.close()


def test_qa_history_is_repository_scoped_sorted_and_tolerates_bad_files(
    tmp_path: Path,
) -> None:
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    service = QAApplicationService(context)
    started = datetime.now(UTC)
    older = QAReport(
        id="qa-older",
        status="completed",
        started_at=started - timedelta(hours=1),
        project_root=str(tmp_path.resolve()),
        summary="Older report",
    )
    newer = QAReport(
        id="qa-newer",
        status="completed",
        started_at=started,
        project_root=str(tmp_path.resolve()),
        summary="Newer report",
    )

    try:
        service._save(older)
        service._save(newer)
        directory = tmp_path / ".daino" / "qa"
        (directory / "qa-broken.json").write_text("not json", encoding="utf-8")
        foreign = newer.model_copy(
            update={"id": "qa-foreign", "project_root": str(tmp_path.parent / "another")}
        )
        (directory / "qa-foreign.json").write_text(foreign.model_dump_json(), encoding="utf-8")

        assert [report.id for report in service.history()] == ["qa-newer", "qa-older"]
        assert service.history(limit=1) == [newer]
        assert service.load("qa-older") == older
        assert service.load("qa-broken") is None
        assert service.load("qa-foreign") is None
    finally:
        context.close()
