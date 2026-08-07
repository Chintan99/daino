"""Validated settings operations."""

from __future__ import annotations

from vasuki.application.context import ProjectContext
from vasuki.config import load_settings, save_settings, set_value


class SettingsApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    def safe_dump(self) -> dict[str, object]:
        return self.context.settings.safe_dump()

    def set(self, key: str, value: str) -> None:
        self.context.settings = set_value(self.context.root, key, value)

    def set_runtime(self, runtime: str, *, persist: bool = False) -> None:
        if runtime not in {"local", "docker", "ssh"}:
            raise ValueError(f"Unknown runtime {runtime}")
        self.context.settings.runtime.default = runtime  # type: ignore[assignment]
        if persist:
            save_settings(self.context.settings, self.context.root)

    def reload(self) -> None:
        self.context.settings = load_settings(self.context.root)
