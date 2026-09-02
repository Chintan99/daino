"""Resolving what to review, and deciding whether it can be merged."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daino.application.context import initialize_project, open_project
from daino.application.review_service import (
    ChangeReviewApplicationService,
    ReviewError,
    evaluate_change_gate,
)
from daino.schemas import ChangedFile, ChangeReview, QAFinding, QASpecialist


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ChangeReviewApplicationService]:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    # This commits everything, including the .gitignore it writes, so a review
    # here starts from a genuinely clean tree.
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    yield ChangeReviewApplicationService(context)
    context.database.engine.dispose()


def _review(**kwargs: object) -> ChangeReview:
    return ChangeReview(
        id="review-1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        subject="the working tree",
        **kwargs,  # type: ignore[arg-type]
    )


def _finding(severity: str, confidence: str = "high") -> QAFinding:
    return QAFinding(
        id=f"f-{severity}-{confidence}",
        title=f"a {severity} problem",
        severity=severity,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- the subject


def test_the_working_tree_review_includes_files_git_has_no_diff_for(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    """A brand-new file is the part of a change most worth reading."""
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "brand_new.py").write_text("def added():\n    return 1\n", encoding="utf-8")

    subject = service.subject("working")
    changes = service._untracked_changes(subject)

    assert "brand_new.py" in subject.untracked
    assert [item.path for item in changes] == ["brand_new.py"]
    assert changes[0].kind == "added"
    assert [line.text for line in changes[0].added] == ["def added():", "    return 1"]


def test_an_ignored_file_is_not_part_of_the_change(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    (tmp_path / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    (tmp_path / "secret.env").write_text("KEY=live\n", encoding="utf-8")

    assert "secret.env" not in service.subject("working").untracked


def test_staged_and_working_are_different_subjects(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    (tmp_path / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    staged = service.subject("staged")
    working = service.subject("working")

    assert "VALUE = 2" in staged.patch
    assert "VALUE = 3" in working.patch
    # Staged reads the index, not the file that has already moved on.
    assert service._reader(staged)("app.py") == "VALUE = 2\n"
    assert service._reader(working)("app.py") == "VALUE = 3\n"


def test_a_branch_review_compares_against_the_base_it_diverged_from(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add the feature")

    subject = service.subject("branch", base_ref="main")

    assert subject.base_ref == "main" and subject.head_ref == "HEAD"
    assert "feature.py" in subject.patch
    assert subject.commits == ("add the feature",)
    assert subject.read_ref == "HEAD"


def test_an_unresolvable_subject_says_why(
    service: ChangeReviewApplicationService,
) -> None:
    with pytest.raises(ReviewError, match="share no history"):
        service.subject("branch", base_ref="0000000000000000000000000000000000000000")
    with pytest.raises(ReviewError, match="ref spec"):
        service.subject("range")


# -------------------------------------------------------------------- the run


async def test_reviewing_an_empty_change_says_so_rather_than_passing_vacuously(
    service: ChangeReviewApplicationService,
) -> None:
    review = await service.run(scope="working")

    assert review.status == "completed"
    assert review.files == []
    assert review.verdict == "pass"
    assert "Nothing to review" in review.gate_reasons[0]


async def test_a_review_records_the_change_and_its_findings(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    (tmp_path / "app.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n",
        encoding="utf-8",
    )

    review = await service.run(scope="working")

    assert review.status == "completed"
    assert [item.path for item in review.files] == ["app.py"]
    assert review.insertions > 0
    references = {item.reference for item in review.findings}
    assert "py-shell-injection" in references
    # The file list carries its own weight, so a reviewer can triage by file.
    assert review.files[0].findings > 0
    # Every family reports, so a clean area says it was looked at.
    assert {item.label for item in review.checks} >= {"Syntax and parsing", "Test coverage"}


async def test_a_review_is_persisted_and_reloadable(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    review = await service.run(scope="working")

    assert service.latest() is not None and service.latest().id == review.id  # type: ignore[union-attr]
    assert service.load(review.id) is not None
    assert [item.id for item in service.history()] == [review.id]
    # A crafted id cannot reach outside the review store.
    assert service.load("../../etc/passwd") is None
    assert service.load("review-../nope") is None


async def test_a_syntax_error_introduced_by_the_change_blocks_it(
    service: ChangeReviewApplicationService, tmp_path: Path
) -> None:
    """The clearest case for a gate: the change does not parse."""
    (tmp_path / "app.py").write_text("VALUE = 1\ndef broken(\n", encoding="utf-8")

    review = await service.run(scope="working")

    assert review.verdict == "blocked"
    assert any("critical" in reason for reason in review.gate_reasons)
    assert next(item for item in review.checks if item.id == "review-syntax").status == "failed"


# --------------------------------------------------------------------- gate


def test_a_critical_finding_blocks_the_merge() -> None:
    verdict, reasons = evaluate_change_gate(
        _review(files=[ChangedFile(path="a.py")], findings=[_finding("critical")])
    )

    assert verdict == "blocked"
    assert "1 critical finding(s)" in reasons[0]


def test_a_fixture_finding_never_blocks() -> None:
    """A credential in a test file is reported, but it is not a blocker."""
    verdict, _ = evaluate_change_gate(
        _review(
            files=[ChangedFile(path="tests/test_a.py")],
            findings=[_finding("critical", confidence="low")],
        )
    )

    assert verdict == "pass"


def test_a_clean_change_says_what_it_checked() -> None:
    from daino.schemas import QACheck

    verdict, reasons = evaluate_change_gate(
        _review(
            files=[ChangedFile(path="a.py", insertions=4)],
            insertions=4,
            deletions=1,
            checks=[
                QACheck(id="c", label="Syntax and parsing", category="security", status="passed")
            ],
        )
    )

    assert verdict == "pass"
    assert "4 added and 1 removed" in reasons[0]
    assert "Syntax and parsing" in reasons[1]


def test_a_reviewer_that_did_not_complete_is_a_warning() -> None:
    """A reviewer that errored looked at nothing, which is not "found nothing"."""
    verdict, reasons = evaluate_change_gate(
        _review(
            files=[ChangedFile(path="a.py")],
            specialists=[
                QASpecialist(id="correctness", label="Correctness", role="reviewer", objective="")
            ],
        )
    )

    assert verdict == "pass"

    failed = _review(
        files=[ChangedFile(path="a.py")],
        specialists=[
            QASpecialist(
                id="correctness",
                label="Correctness",
                role="reviewer",
                objective="",
                status="failed",
            )
        ],
    )
    verdict, reasons = evaluate_change_gate(failed)
    assert verdict == "warn"
    assert "did not complete" in reasons[0]


def test_an_unfinished_review_clears_nothing() -> None:
    assert evaluate_change_gate(_review(status="cancelled"))[0] == "unknown"
    assert evaluate_change_gate(_review(status="failed"))[0] == "unknown"


def test_a_blocked_change_still_says_when_reviewers_did_not_read_it() -> None:
    """Otherwise a blocked change reads as fully reviewed when half was skipped."""
    verdict, reasons = evaluate_change_gate(
        _review(
            files=[ChangedFile(path="a.py")],
            findings=[_finding("critical")],
            specialists=[
                QASpecialist(
                    id="correctness",
                    label="Correctness",
                    role="reviewer",
                    objective="",
                    status="failed",
                )
            ],
        )
    )

    assert verdict == "blocked"
    assert "critical" in reasons[0]
    assert "did not complete" in reasons[-1]
