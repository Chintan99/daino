"""Versioned dynamic playbook discovery and validation."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Playbook(BaseModel):
    name: str
    version: str
    purpose: str
    preconditions: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str]
    execution_stages: list[str]
    approval_points: list[str] = Field(default_factory=list)
    verification_steps: list[str]
    rollback_steps: list[str]


class PlaybookLoader:
    def __init__(self, project_root: Path) -> None:
        self.directories = [
            Path(__file__).parent / "builtin",
            project_root / ".vasuki" / "playbooks",
        ]

    def list(self) -> list[Playbook]:
        items: dict[str, Playbook] = {}
        for directory in self.directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.yaml")):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                playbook = Playbook.model_validate(data)
                items[playbook.name] = playbook
        return sorted(items.values(), key=lambda item: item.name)

    def get(self, name: str) -> Playbook:
        for playbook in self.list():
            if playbook.name == name:
                return playbook
        raise ValueError(f"Unknown playbook {name}")
