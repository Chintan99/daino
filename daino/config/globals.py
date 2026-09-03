"""User-level configuration shared by every project.

Which model you use and which provider it comes from are facts about *you*, not
about a repository. Keeping them per-project meant onboarding ran again in every
new directory and the same API key was pasted repeatedly, so opening Daino
somewhere new was a setup task rather than a start.

Configuration is therefore two layers. The global file holds what follows you
between projects — providers, model profiles, routing, and interface preferences.
The project file holds what is genuinely local — its name, its database, its
verification commands, its security policy — and overrides the global layer where
the two disagree, so a repository can still pin a different model deliberately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from daino.config import paths
from daino.config.models import Settings

#: Sections that follow the user between projects. Everything not listed here is
#: project-local and is never written to the global file: a repository's name,
#: database, or verification commands mean nothing in another checkout.
GLOBAL_SECTIONS = (
    "providers",
    "models",
    "routing",
    "routing_fallbacks",
    "tui",
    "observability",
    "memory",
    "notifications",
)


def global_config_dir() -> Path:
    """Directory holding user-level configuration and secrets.

    Honours ``DAINO_CONFIG_HOME`` (then legacy ``VASUKI_CONFIG_HOME``), then
    ``XDG_CONFIG_HOME``, then the conventional ``~/.config``. Deliberately not
    ``~/.daino``: that path is a *project*-style marker, so writing user settings
    there would make the home directory look like a repository to every
    project-root search. Delegates to :mod:`daino.config.paths`, which also falls
    back to a pre-existing ``vasuki`` directory.
    """
    return paths.global_config_dir()


def global_config_path() -> Path:
    return global_config_dir() / "config.yaml"


def load_global_data() -> dict[str, Any]:
    """Read the global layer as raw data, or an empty mapping when unset.

    Returns data rather than ``Settings`` because it is a partial layer: it is
    merged under a project's own values before anything is validated.
    """
    path = global_config_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        # A damaged global file must not stop every project from opening.
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in GLOBAL_SECTIONS}


def has_global_provider() -> bool:
    """Whether a usable model is already configured for every project."""
    data = load_global_data()
    return bool(data.get("providers")) and bool(data.get("models"))


def merge_layers(global_data: dict[str, Any], project_data: dict[str, Any]) -> dict[str, Any]:
    """Combine the two layers, with the project winning.

    Merged one level deep so a project can add a provider or repoint a single
    role without restating the whole global block; a project key that names the
    same provider replaces it outright rather than being blended, because a
    half-merged provider is not a thing anyone wants.
    """
    merged: dict[str, Any] = dict(global_data)
    for key, value in project_data.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            combined = dict(existing)
            combined.update(value)
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def save_global(settings: Settings) -> Path:
    """Write the global sections of ``settings``, leaving project-local ones out."""
    directory = global_config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        key: value
        for key, value in settings.safe_dump().items()
        if key in GLOBAL_SECTIONS and value
    }
    path = global_config_path()
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    path.chmod(0o600)
    return path
