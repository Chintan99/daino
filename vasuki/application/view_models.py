"""Serializable view models used by Textual and other presentation layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MissionSummary:
    id: str
    title: str
    status: str
    mode: str
    updated_at: datetime
    branch: str = ""
    workspace: str = ""
    task_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationItem:
    id: str
    kind: str
    role: str
    content: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FileItem:
    path: str
    language: str
    status: str = ""
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    type: str
    base_url: str
    model: str
    connected: bool | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class OpenRouterModel:
    id: str
    name: str
    context_length: int = 0
    prompt_price: str = ""
    completion_price: str = ""

    @property
    def label(self) -> str:
        context = f" · {self.context_length:,} ctx" if self.context_length else ""
        return f"{self.name}  [{self.id}]{context}"
