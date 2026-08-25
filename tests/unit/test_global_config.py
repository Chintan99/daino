"""Configuration is layered: user-level globals under project-local overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vasuki.config import find_project_root, load_settings, save_settings
from vasuki.config.globals import (
    GLOBAL_SECTIONS,
    global_config_dir,
    global_config_path,
    has_global_provider,
    load_global_data,
    merge_layers,
    save_global,
)
from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings


def configured() -> Settings:
    settings = Settings(project={"name": "demo"})
    settings.providers = {
        "openrouter": ProviderConfig(
            type="openrouter", base_url="https://openrouter.ai/api/v1", model="gpt-5.6"
        )
    }
    settings.models = {"openrouter": ModelProfileConfig(provider="openrouter", model="gpt-5.6")}
    settings.routing = {"builder": "openrouter"}
    return settings


def project_at(root: Path) -> Path:
    (root / ".vasuki").mkdir(parents=True, exist_ok=True)
    return root


def test_explicit_project_directory_is_not_captured_by_parent(tmp_path: Path) -> None:
    parent = project_at(tmp_path / "parent")
    (parent / ".git").mkdir()
    child = parent / "test2"
    child.mkdir()

    assert find_project_root(child) == child


def test_implicit_cli_discovery_still_uses_the_git_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    child = repository / "src" / "package"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    assert find_project_root() == repository


# --------------------------------------------------------------------------
# Where globals live
# --------------------------------------------------------------------------


def test_global_config_honours_the_override_then_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("VASUKI_CONFIG_HOME", str(explicit))
    assert global_config_dir() == explicit

    monkeypatch.delenv("VASUKI_CONFIG_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert global_config_dir() == tmp_path / "xdg" / "vasuki"


def test_globals_do_not_live_in_a_directory_that_looks_like_a_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``~/.vasuki`` is a project marker; user settings there make $HOME a repo."""
    monkeypatch.delenv("VASUKI_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert global_config_dir() != Path.home() / ".vasuki"
    assert global_config_dir().name == "vasuki"


# --------------------------------------------------------------------------
# Layering
# --------------------------------------------------------------------------


def test_a_project_without_config_inherits_the_global_model(tmp_path: Path) -> None:
    """The whole point: a new directory needs no configuration of its own."""
    save_global(configured())
    settings = load_settings(project_at(tmp_path), require=False)

    assert "openrouter" in settings.providers
    assert settings.routing["builder"] == "openrouter"


def test_a_project_may_still_override_the_global_choice(tmp_path: Path) -> None:
    save_global(configured())
    root = project_at(tmp_path)
    (root / ".vasuki" / "config.yaml").write_text(
        yaml.safe_dump({"routing": {"builder": "local"}}), encoding="utf-8"
    )

    settings = load_settings(root)

    # The project wins where it speaks...
    assert settings.routing["builder"] == "local"
    # ...and inherits everything it does not mention.
    assert "openrouter" in settings.providers


def test_a_project_file_does_not_duplicate_global_values(tmp_path: Path) -> None:
    """Copying globals into each project freezes today's model into every repo."""
    save_global(configured())
    root = project_at(tmp_path)

    save_settings(load_settings(root, require=False), root)

    written = yaml.safe_load((root / ".vasuki" / "config.yaml").read_text(encoding="utf-8"))
    assert "providers" not in written
    assert "project" in written


def test_changing_the_global_model_reaches_existing_projects(tmp_path: Path) -> None:
    """The reason duplication matters: a later change must actually take effect."""
    save_global(configured())
    root = project_at(tmp_path)
    save_settings(load_settings(root, require=False), root)

    switched = configured()
    switched.routing = {"builder": "something-else"}
    save_global(switched)

    assert load_settings(root).routing["builder"] == "something-else"


def test_only_user_level_sections_are_written_globally(tmp_path: Path) -> None:
    """A repository's name, database, or verification commands are not portable."""
    settings = configured()
    settings.verification.commands = ["pytest -q"]

    save_global(settings)
    written = yaml.safe_load(global_config_path().read_text(encoding="utf-8"))

    assert set(written) <= set(GLOBAL_SECTIONS)
    for local_only in ("project", "database", "verification", "git", "security"):
        assert local_only not in written


def test_a_damaged_global_file_does_not_stop_projects_opening(tmp_path: Path) -> None:
    global_config_dir().mkdir(parents=True, exist_ok=True)
    global_config_path().write_text("{{{ not yaml", encoding="utf-8")

    assert load_global_data() == {}
    assert not has_global_provider()


def test_has_global_provider_requires_both_a_provider_and_a_profile() -> None:
    assert not has_global_provider()
    save_global(configured())
    assert has_global_provider()


@pytest.mark.parametrize(
    ("global_data", "project_data", "expected"),
    [
        ({"routing": {"builder": "a"}}, {}, {"builder": "a"}),
        ({"routing": {"builder": "a"}}, {"routing": {"builder": "b"}}, {"builder": "b"}),
        # A project adding a role keeps the global ones it did not mention.
        (
            {"routing": {"builder": "a"}},
            {"routing": {"tester": "t"}},
            {"builder": "a", "tester": "t"},
        ),
    ],
)
def test_merge_is_one_level_deep(
    global_data: dict[str, object],
    project_data: dict[str, object],
    expected: dict[str, str],
) -> None:
    assert merge_layers(global_data, project_data)["routing"] == expected
