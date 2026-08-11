from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import git
from vasuki.cli.app import app
from vasuki.workspace import WorkspaceManager


def test_git_worktree_and_checkpoint_round_trip(git_repo: Path) -> None:
    manager = WorkspaceManager(git_repo)
    workspace = manager.create("mission-test", "change readme")
    assert workspace.path.exists()
    assert workspace.branch.startswith("vasuki/mission-test/")
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
    assert (tmp_path / ".vasuki" / "config.yaml").exists()
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
