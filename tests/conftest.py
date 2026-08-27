from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.config import default_settings, save_settings
from daino.persistence import Database


@pytest.fixture(autouse=True)
def host_runtime_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Initialize test projects with the host runtime whatever Docker is doing.

    ``initialize_project`` probes the Docker daemon and records it when it
    answers. Left unpatched, every test that initializes a project would behave
    one way on a machine running Docker and another way without it. The runtime
    probe itself is tested directly in ``test_agent_shell``.
    """
    from daino.application import context as context_module

    monkeypatch.setattr(context_module, "preferred_runtime", lambda: "local")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[tuple[Path, object, Database]]:
    settings = default_settings(tmp_path)
    settings.runtime.default = "local"
    settings.verification.require_review = False
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield tmp_path, settings, database
    database.engine.dispose()


def git(root: Path, *args: str) -> str:
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
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text(".daino/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def painted_text(app: object) -> str:
    """Return the characters actually composited to the terminal.

    Preferred over ``export_screenshot``: the SVG exporter splits a row into
    several ``<text>`` runs at unpredictable points, so substring assertions
    against its markup fail even when the text is plainly on screen.
    """
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()  # type: ignore[attr-defined]
    )


def commit_all(root: Path) -> None:
    """Make a directory a Git repository with everything committed.

    Coding missions require one, so a realistic TUI fixture has one too.
    """
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")


@pytest.fixture(autouse=True)
def no_desktop_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never notify the developer's desktop or inhibit their machine's sleep.

    A test that finishes a turn would otherwise raise a real OS notification and
    spawn a real `caffeinate`, hundreds of times per run. Tests that exercise
    those features turn the switches back on deliberately and stub the commands.
    """
    monkeypatch.setenv("DAINO_NOTIFY", "off")
    monkeypatch.setenv("DAINO_WAKELOCK", "off")


@pytest.fixture(autouse=True)
def isolated_global_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point global configuration at a scratch directory for every test.

    Configuration is user-level, so without this a test that connects a provider
    writes into the developer's real ~/.config/daino and every later test —
    and the developer's own installation — inherits a provider pointing at a
    dead port. Autouse because the risk applies to any test that touches
    settings, not only the ones that obviously do.
    """
    monkeypatch.setenv("DAINO_CONFIG_HOME", str(tmp_path_factory.mktemp("daino-global")))
