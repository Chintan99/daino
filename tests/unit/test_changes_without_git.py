"""A directory that was never ``git init``-ed still has work worth showing.

The regression this guards: the Changes view called ``git diff`` unconditionally
and printed "Diff unavailable: fatal: not a git repository". The agent's edits
are recorded as they happen, so the files it created can be listed instead of an
error the user can do nothing useful with.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from vasuki.application import RepositoryApplicationService, initialize_project, open_project
from vasuki.persistence.models import ToolCall
from vasuki.utils.ids import new_id


def service_for(root: Path) -> RepositoryApplicationService:
    initialize_project(root)
    return RepositoryApplicationService(open_project(root))


def record_write(service: RepositoryApplicationService, path: str, *, success: bool = True) -> None:
    with service.context.database.session() as session:
        session.add(
            ToolCall(
                id=new_id("tool-call"),
                mission_id="mission-1",
                tool="chat.write",
                arguments={"path": path},
                result_summary="ok",
                duration_seconds=0.0,
                success=success,
            )
        )


def test_written_files_are_listed_with_their_size(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    (tmp_path / "books-data.js").write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
    record_write(service, "books-data.js")

    written = service.written_files("mission-1")

    assert [item["path"] for item in written] == ["books-data.js"]
    assert written[0]["lines"] == 2
    assert written[0]["exists"] is True
    assert written[0]["action"] == "write"


def test_a_failed_write_is_not_reported_as_a_change(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    record_write(service, "never-landed.js", success=False)

    assert service.written_files("mission-1") == []


def test_repeated_writes_to_one_file_are_listed_once(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    (tmp_path / "app.js").write_text("x\n", encoding="utf-8")
    record_write(service, "app.js")
    record_write(service, "app.js")

    assert len(service.written_files("mission-1")) == 1


def test_a_directory_without_git_is_reported_as_such_not_as_an_error(tmp_path: Path) -> None:
    """``has_git`` is what lets the view choose a file list over a Git failure."""
    service = service_for(tmp_path)
    assert service.has_git() is True

    shutil.rmtree(tmp_path / ".git")
    assert service.has_git() is False
    # And the listing still works, which is the whole point.
    (tmp_path / "index.html").write_text("<h1>hi</h1>\n", encoding="utf-8")
    record_write(service, "index.html")
    assert [item["path"] for item in service.written_files("mission-1")] == ["index.html"]


def test_a_mission_in_a_bare_directory_initializes_git_rather_than_refusing(
    tmp_path: Path,
) -> None:
    """Refusing the work outright made a missing ``git init`` look like a failure."""
    from vasuki.git import GitClient

    (tmp_path / "index.html").write_text("<h1>hi</h1>\n", encoding="utf-8")
    git = GitClient(tmp_path)
    assert git.is_repository() is False

    assert git.ensure_repository() is True
    assert git.is_repository() is True
    assert git.revision(), "checkpoints need a revision to anchor to"
