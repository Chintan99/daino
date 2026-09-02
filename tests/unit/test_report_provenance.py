"""A verdict is a statement about code, so it has to say which code.

Both Inspector reports used to record the project root and nothing else, which
meant "safe to push" outlived the checkout that earned it: the tab badge kept
reassuring people about files nobody had inspected. And a saved review re-derived
its diff from the current working tree, so last week's findings were rendered
beside code written since.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daino.application import initialize_project, open_project
from daino.application.context import ProjectContext
from daino.application.qa_service import QAApplicationService
from daino.application.review_service import ChangeReviewApplicationService
from daino.git import GitClient


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def project(tmp_path: Path) -> Iterator[ProjectContext]:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text(".daino/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    yield context
    context.close()


def test_the_fingerprint_moves_when_the_working_tree_does(project: ProjectContext) -> None:
    """Uncommitted edits have to change it: most inspections run on a dirty tree."""
    git = GitClient(project.root)
    before = git.checkout_fingerprint()

    (project.root / "app.py").write_text("value = 2\n", encoding="utf-8")
    after = git.checkout_fingerprint()

    assert before["commit"] == after["commit"]
    assert before["digest"] != after["digest"]
    assert after["dirty"] is True

    # A new untracked file counts too, or "add the secret, then push" slips by.
    (project.root / "secrets.env").write_text("TOKEN=abc\n", encoding="utf-8")
    assert GitClient(project.root).checkout_fingerprint()["digest"] != after["digest"]


def test_a_verdict_stops_being_current_once_the_code_changes(
    project: ProjectContext,
) -> None:
    service = QAApplicationService(project)
    report = _report(service)

    assert service.is_current(report)

    (project.root / "app.py").write_text("value = 3\n", encoding="utf-8")

    assert not service.is_current(report)


def test_an_unpinnable_report_is_never_reported_as_current(
    project: ProjectContext,
) -> None:
    """A clearance nobody can verify is not a clearance.

    Covers both a report written before reports were pinned and one taken
    somewhere Git cannot answer.
    """
    service = QAApplicationService(project)
    report = _report(service)
    report.checkout = report.checkout.model_copy(update={"digest": ""})

    assert not service.is_current(report)
    assert not service.is_current(None)


@pytest.mark.asyncio
async def test_a_review_keeps_the_patch_it_reviewed(project: ProjectContext) -> None:
    """The findings and the diff have to age together."""
    (project.root / "app.py").write_text("value = 1\nvalue = 99\n", encoding="utf-8")
    service = ChangeReviewApplicationService(project)

    review = await service.run(scope="working")

    assert "value = 99" in review.patch
    assert review.checkout.digest

    # The tree moves on; the saved review must not follow it.
    (project.root / "app.py").write_text("value = 1\nsomething_else = 0\n", encoding="utf-8")
    reloaded = service.load(review.id)
    assert reloaded is not None
    assert "value = 99" in reloaded.patch
    assert "something_else" not in reloaded.patch
    assert reloaded.checkout.digest != GitClient(project.root).checkout_fingerprint()["digest"]


def _report(service: QAApplicationService):
    from daino.schemas import QAReport

    return QAReport(
        id="qa-1",
        status="completed",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        checkout=service.checkout(),
    )
