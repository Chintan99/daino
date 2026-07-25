from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from vasuki.config import default_settings, save_settings
from vasuki.persistence import Database


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
    (tmp_path / ".gitignore").write_text(".vasuki/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path
