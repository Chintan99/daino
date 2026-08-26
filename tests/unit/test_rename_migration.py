"""Rename (Vasuki → Daino) and storage-migration guarantees."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from typer.testing import CliRunner

import daino
from daino.cli.app import _rewrite_leading_path, app
from daino.config import paths
from daino.config.models import Settings
from daino.persistence.database import normalized_database_url


def test_daino_package_imports_and_versioned() -> None:
    assert isinstance(daino.__version__, str) and daino.__version__


def test_vasuki_import_shim_resolves_to_daino_and_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from vasuki.agents import ToolLoop

        import vasuki  # noqa: F401 - exercising the deprecation shim

    import daino.agents

    assert vasuki is daino
    assert ToolLoop is daino.agents.ToolLoop
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_daino_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("daino ")


def test_leading_path_is_rewritten_but_subcommands_are_not() -> None:
    assert _rewrite_leading_path([]) == []
    assert _rewrite_leading_path(["."]) == ["--project", "."]
    assert _rewrite_leading_path([".", "--gui"]) == ["--project", ".", "--gui"]
    assert _rewrite_leading_path(["--gui"]) == ["--gui"]
    assert _rewrite_leading_path(["config", "show"]) == ["config", "show"]


def test_project_state_prefers_daino_but_falls_back_to_legacy(tmp_path: Path) -> None:
    # Fresh project writes to .daino.
    assert paths.state_dir(tmp_path).name == ".daino"

    legacy = tmp_path / "legacy"
    (legacy / ".vasuki").mkdir(parents=True)
    assert paths.state_dir(legacy).name == ".vasuki"  # legacy is used in place

    both = tmp_path / "both"
    (both / ".vasuki").mkdir(parents=True)
    (both / ".daino").mkdir(parents=True)
    assert paths.state_dir(both).name == ".daino"  # new wins when both exist


def test_legacy_database_is_read_in_place(tmp_path: Path) -> None:
    (tmp_path / ".vasuki").mkdir()
    (tmp_path / ".vasuki" / "vasuki.db").write_text("", encoding="utf-8")
    url = normalized_database_url(Settings(), tmp_path)
    assert url.endswith("/.vasuki/vasuki.db")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert normalized_database_url(Settings(), fresh).endswith("/.daino/daino.db")


def test_global_dirs_honour_daino_then_vasuki_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VASUKI_CONFIG_HOME", raising=False)
    monkeypatch.setenv("DAINO_CONFIG_HOME", str(tmp_path / "daino-home"))
    assert paths.global_config_dir() == tmp_path / "daino-home"

    monkeypatch.delenv("DAINO_CONFIG_HOME")
    monkeypatch.setenv("VASUKI_CONFIG_HOME", str(tmp_path / "vasuki-home"))
    assert paths.global_config_dir() == tmp_path / "vasuki-home"


def test_instruction_file_prefers_daino_then_legacy(tmp_path: Path) -> None:
    from daino.memory.instructions import _instruction_in

    directory = tmp_path
    assert _instruction_in(directory).name == "DAINO.md"  # default when neither exists

    (directory / "VASUKI.md").write_text("legacy\n", encoding="utf-8")
    assert _instruction_in(directory).name == "VASUKI.md"  # legacy read when only it exists

    (directory / "DAINO.md").write_text("new\n", encoding="utf-8")
    assert _instruction_in(directory).name == "DAINO.md"  # new wins
