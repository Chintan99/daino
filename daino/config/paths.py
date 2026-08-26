"""Canonical project/global storage locations with legacy-Vasuki fallback.

Daino was renamed from Vasuki. New state lives under ``.daino`` (project) and
``~/.daino`` / ``~/.config/daino`` (global), but an existing checkout may still
carry ``.vasuki`` state and a user may still have ``~/.vasuki`` / ``~/.config/vasuki``.

The policy is *read-legacy / write-new*:

* A brand-new project writes to ``.daino``.
* A project that already has ``.daino`` uses it.
* A project that only has legacy ``.vasuki`` keeps using it in place, so its
  sessions, database, and configuration remain a single consolidated store
  rather than being split across two directories or silently abandoned.

Legacy data is never moved or deleted automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Canonical (new) project state directory name.
STATE_DIR = ".daino"
#: Legacy (Vasuki) project state directory name, still read when present.
LEGACY_STATE_DIR = ".vasuki"
#: Both markers, newest first — used for project detection and ignore lists.
STATE_DIR_NAMES: tuple[str, str] = (STATE_DIR, LEGACY_STATE_DIR)

#: SQLite database filename inside the canonical / legacy state directory.
DB_FILENAME = "daino.db"
LEGACY_DB_FILENAME = "vasuki.db"

#: The default (relative) database URL stored in configuration.
DEFAULT_DATABASE_URL = f"sqlite:///{STATE_DIR}/{DB_FILENAME}"
#: The historical default, still recognised so old config files keep working.
LEGACY_DATABASE_URL = f"sqlite:///{LEGACY_STATE_DIR}/{LEGACY_DB_FILENAME}"

#: Canonical global-config directory name (under XDG_CONFIG_HOME / ~/.config).
GLOBAL_DIR_NAME = "daino"
LEGACY_GLOBAL_DIR_NAME = "vasuki"

#: Global procedural-memory filename discovered hierarchically in a repo.
INSTRUCTION_FILENAME = "DAINO.md"
LEGACY_INSTRUCTION_FILENAME = "VASUKI.md"


def state_dir(root: Path, *, create: bool = False) -> Path:
    """Return the project state directory, honouring the legacy fallback.

    Prefers an existing ``.daino``; falls back to a pre-existing ``.vasuki`` so a
    legacy project stays consolidated; otherwise defaults to ``.daino``. Pass
    ``create=True`` to create the chosen directory.
    """
    root = Path(root)
    daino_dir = root / STATE_DIR
    legacy_dir = root / LEGACY_STATE_DIR
    if daino_dir.exists():
        chosen = daino_dir
    elif legacy_dir.exists():
        chosen = legacy_dir
    else:
        chosen = daino_dir
    if create:
        chosen.mkdir(parents=True, exist_ok=True)
    return chosen


def state_path(root: Path, *parts: str, create_parents: bool = False) -> Path:
    """Return a path inside the resolved project state directory."""
    path = state_dir(root).joinpath(*parts)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def is_project(root: Path) -> bool:
    """Whether ``root`` has been initialised (new or legacy marker present)."""
    root = Path(root)
    return any((root / name).exists() for name in STATE_DIR_NAMES)


def resolved_database_file(root: Path) -> Path:
    """Absolute path to the project SQLite database, honouring legacy fallback.

    ``.daino/daino.db`` is preferred; a pre-existing legacy ``.vasuki/vasuki.db``
    is used in place so existing sessions and history stay accessible.
    """
    root = Path(root).resolve()
    daino_db = root / STATE_DIR / DB_FILENAME
    legacy_db = root / LEGACY_STATE_DIR / LEGACY_DB_FILENAME
    if daino_db.exists():
        return daino_db
    if legacy_db.exists():
        return legacy_db
    return daino_db


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def global_config_dir() -> Path:
    """User-level configuration directory.

    Honours ``DAINO_CONFIG_HOME`` (then legacy ``VASUKI_CONFIG_HOME``), then
    ``XDG_CONFIG_HOME``, then ``~/.config``. Prefers ``daino``; falls back to a
    pre-existing ``vasuki`` directory so existing global settings keep loading.
    """
    override = _env("DAINO_CONFIG_HOME", "VASUKI_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    daino_dir = (root / GLOBAL_DIR_NAME).resolve()
    legacy_dir = (root / LEGACY_GLOBAL_DIR_NAME).resolve()
    if not daino_dir.exists() and legacy_dir.exists():
        return legacy_dir
    return daino_dir


def global_memory_dir() -> Path:
    """Private user-memory directory.

    Honours ``DAINO_HOME`` / ``VASUKI_HOME`` (then the config-home overrides).
    Prefers ``~/.daino``; falls back to a pre-existing ``~/.vasuki``.
    """
    override = _env("DAINO_HOME", "VASUKI_HOME", "DAINO_CONFIG_HOME", "VASUKI_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    daino_dir = (Path.home() / ".daino").resolve()
    legacy_dir = (Path.home() / ".vasuki").resolve()
    if not daino_dir.exists() and legacy_dir.exists():
        return legacy_dir
    return daino_dir


def global_instruction_path() -> Path:
    """Path to the global procedural-memory file (``DAINO.md``, legacy ``VASUKI.md``)."""
    directory = global_memory_dir()
    daino_file = directory / INSTRUCTION_FILENAME
    legacy_file = directory / LEGACY_INSTRUCTION_FILENAME
    if not daino_file.exists() and legacy_file.exists():
        return legacy_file
    return daino_file


def instruction_filenames() -> tuple[str, str]:
    """Both instruction filenames, newest first (for hierarchical discovery)."""
    return (INSTRUCTION_FILENAME, LEGACY_INSTRUCTION_FILENAME)
