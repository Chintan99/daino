from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from daino.application import open_project
from daino.cli.app import app
from daino.config import default_settings, save_settings
from daino.workspace import WorkspaceManager
from tests.conftest import git


def test_git_worktree_and_checkpoint_round_trip(git_repo: Path) -> None:
    manager = WorkspaceManager(git_repo)
    workspace = manager.create("mission-test", "change readme")
    assert workspace.path.exists()
    assert workspace.branch.startswith("daino/mission-test/")
    changed = workspace.path / "README.md"
    changed.write_text("changed\n", encoding="utf-8")
    _, archive = manager.checkpoint(workspace, "changed state")
    changed.write_text("other\n", encoding="utf-8")
    manager.restore_checkpoint(archive, workspace.path)
    assert changed.read_text(encoding="utf-8") == "changed\n"
    manager.cleanup(workspace, discard=True)
    assert not workspace.path.exists()


def test_worktree_records_dirty_original_checkout(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("user change\n", encoding="utf-8")
    (git_repo / "untracked.txt").write_text("user data\n", encoding="utf-8")
    workspace = WorkspaceManager(git_repo).create("mission-dirty", "safe edit")
    assert "README.md" in workspace.original_status
    assert "untracked.txt" in workspace.original_status
    assert not (workspace.path / "untracked.txt").exists()
    WorkspaceManager(git_repo).cleanup(workspace, discard=True)


def test_cli_init_config_and_repository_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "hello.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".daino" / "config.yaml").exists()
    assert runner.invoke(app, ["config", "validate"]).exit_code == 0
    symbols = runner.invoke(app, ["repo", "symbols"])
    assert symbols.exit_code == 0
    assert "hello" in symbols.output
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "deploy" in help_result.output


def test_cli_init_bootstraps_a_greenfield_git_baseline(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".git").is_dir()
    assert git(tmp_path, "rev-parse", "--verify", "HEAD")
    assert git(tmp_path, "log", "-1", "--format=%s") == "Initialize project"


def test_cli_init_isolates_a_directory_nested_in_a_parent_repository(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-b", "main")
    child = parent / "test1"
    child.mkdir()
    runner = CliRunner()

    result = runner.invoke(app, ["init", str(child)])

    assert result.exit_code == 0, result.output
    assert (child / ".daino" / "config.yaml").exists()
    assert (child / ".git").is_dir()
    assert Path(git(child, "rev-parse", "--show-toplevel")) == child.resolve()
    assert git(child, "rev-parse", "--verify", "HEAD")


def test_cli_init_creates_head_when_initial_files_are_ignored(tmp_path: Path) -> None:
    runner = CliRunner()
    git(tmp_path, "init", "-b", "main")
    (tmp_path / ".gitignore").write_text("*\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert git(tmp_path, "rev-parse", "--verify", "HEAD")
    assert git(tmp_path, "log", "-1", "--format=%s") == "Initialize project"


def test_open_repairs_child_initialized_by_parent_scoped_version(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-b", "main")
    child = parent / "test1"
    child.mkdir()
    (child / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    save_settings(default_settings(child), child)
    assert not (child / ".git").exists()

    context = open_project(child)
    context.close()

    assert (child / ".git").is_dir()
    assert Path(git(child, "rev-parse", "--show-toplevel")) == child.resolve()
    assert git(child, "rev-parse", "--verify", "HEAD")
