"""YAML configuration loading, environment overrides, and safe updates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vasuki.config.models import Settings
from vasuki.exceptions import ConfigurationError

CONFIG_DIR = ".vasuki"
CONFIG_FILE = "config.yaml"


def find_project_root(start: Path | None = None) -> Path:
    """Find the closest Git or Vasuki project root."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / CONFIG_DIR).exists():
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
    if os.getenv("VLLM_API_KEY"):
        data.setdefault("providers", {}).setdefault("vllm", {}).setdefault(
            "api_key", "env://VLLM_API_KEY"
        )
    return data


def load_settings(root: Path | None = None, *, require: bool = True) -> Settings:
    """Load and validate project settings."""
    path = config_path(root)
    if not path.exists():
        if require:
            raise ConfigurationError(f"No {CONFIG_DIR}/{CONFIG_FILE}; run `vasuki init` first")
        return default_settings(find_project_root(root))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Settings.model_validate(_apply_environment(raw))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def save_settings(settings: Settings, root: Path | None = None) -> Path:
    """Persist settings without secret material."""
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(settings.safe_dump(), sort_keys=False, allow_unicode=True)
    path.write_text(rendered, encoding="utf-8")
    return path


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
