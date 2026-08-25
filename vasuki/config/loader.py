"""YAML configuration loading, environment overrides, and safe updates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vasuki.config.globals import GLOBAL_SECTIONS, load_global_data, merge_layers
from vasuki.config.models import Settings
from vasuki.exceptions import ConfigurationError

CONFIG_DIR = ".vasuki"
CONFIG_FILE = "config.yaml"


def find_project_root(start: Path | None = None) -> Path:
    """Resolve a workspace, treating an explicitly supplied path as authoritative.

    ``start`` is used by the TUI and ``--project`` flag to express the workspace
    the user actually chose. It must not be replaced by a parent Git/Vasuki
    directory: doing so reopens that parent's database, history, and usage in a
    newly created child directory. Calls without ``start`` retain repository
    discovery for CLI commands invoked from an existing checkout subdirectory.
    """
    if start is not None:
        return start.resolve()

    current = Path.cwd().resolve()
    if (current / CONFIG_DIR).exists():
        return current
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def config_path(root: Path | None = None) -> Path:
    return find_project_root(root) / CONFIG_DIR / CONFIG_FILE


def default_settings(root: Path) -> Settings:
    return Settings(project={"name": root.name})


def _apply_environment(data: dict[str, Any]) -> dict[str, Any]:
    env_map = {
        "DATABASE_URL": ("database", "url"),
        "VASUKI_RUNTIME": ("runtime", "default"),
        "OPENROUTER_BASE_URL": ("providers", "openrouter", "base_url"),
        "OPENROUTER_MODEL": ("providers", "openrouter", "model"),
        "OLLAMA_BASE_URL": ("providers", "ollama", "base_url"),
        "OLLAMA_MODEL": ("providers", "ollama", "model"),
        "VLLM_BASE_URL": ("providers", "vllm", "base_url"),
        "VLLM_MODEL": ("providers", "vllm", "model"),
    }
    for env_name, path in env_map.items():
        value = os.getenv(env_name)
        if value is None:
            continue
        cursor = data
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value
    if os.getenv("OPENROUTER_API_KEY"):
        data.setdefault("providers", {}).setdefault("openrouter", {}).setdefault(
            "api_key", "env://OPENROUTER_API_KEY"
        )
    if os.getenv("OLLAMA_API_KEY"):
        data.setdefault("providers", {}).setdefault("ollama", {}).setdefault(
            "api_key", "env://OLLAMA_API_KEY"
        )
    if os.getenv("VLLM_API_KEY"):
        data.setdefault("providers", {}).setdefault("vllm", {}).setdefault(
            "api_key", "env://VLLM_API_KEY"
        )
    return data


def load_settings(root: Path | None = None, *, require: bool = True) -> Settings:
    """Load and validate settings, layering the project over the user's globals.

    Providers, models, and routing come from the global file unless the project
    overrides them, so a model configured once is available in every checkout
    without repeating onboarding.
    """
    path = config_path(root)
    resolved_root = find_project_root(root)
    global_data = load_global_data()
    if not path.exists():
        if require and not global_data:
            raise ConfigurationError(f"No {CONFIG_DIR}/{CONFIG_FILE}; run `vasuki init` first")
        base = default_settings(resolved_root).safe_dump()
        merged = merge_layers(global_data, {"project": base["project"]})
        try:
            return Settings.model_validate(_apply_environment(merged))
        except ValidationError as exc:
            raise ConfigurationError(f"Invalid configuration: {exc}") from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        merged = merge_layers(global_data, raw if isinstance(raw, dict) else {})
        return Settings.model_validate(_apply_environment(merged))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def save_settings(settings: Settings, root: Path | None = None) -> Path:
    """Persist project settings without secret material.

    Values identical to the global layer are left out. Copying them into every
    project would freeze today's model into each repository, so changing the
    global choice later would silently fail to reach any existing project.
    """
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.safe_dump()
    global_data = load_global_data()
    for key in GLOBAL_SECTIONS:
        # Drop a section that matches the global layer, and an empty one that
        # only exists because the model has a default. Either way the project
        # should inherit rather than pin, so a later global change reaches it.
        if key in data and (data[key] == global_data.get(key) or not data[key]):
            data.pop(key)
    rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(rendered, encoding="utf-8")
    return path


def use_global_provider_settings(root: Path) -> Settings:
    """Remove project provider/model/routing overrides and reload globals."""
    path = config_path(root)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = raw if isinstance(raw, dict) else {}
    for key in ("providers", "models", "routing", "routing_fallbacks"):
        data.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return load_settings(root)


def set_value(root: Path, dotted_key: str, raw_value: str) -> Settings:
    """Set a dotted configuration key, validating the complete result."""
    path = config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cursor = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = yaml.safe_load(raw_value)
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc
    save_settings(settings, root)
    return settings
