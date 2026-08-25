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
class CatalogModel:
    """One model a provider reports that it actually offers.

    Filled from OpenRouter's catalog and from a local Ollama's installed models,
    so the model is picked from what is really there rather than typed from
    memory and discovered to be wrong at the first request.
    """

    id: str
    name: str
    context_length: int = 0
    prompt_price: str = ""
    completion_price: str = ""
    #: Provider-specific extras worth seeing while choosing — an Ollama model's
    #: on-disk size and capabilities, for instance.
    detail: str = ""

    @property
    def label(self) -> str:
        # Ollama names a model by its tag, so the identifier would otherwise be
        # printed twice: "qwen3:27b  [qwen3:27b]".
        head = self.name if self.name == self.id else f"{self.name}  [{self.id}]"
        context = f" · {self.context_length:,} ctx" if self.context_length else ""
        extra = f" · {self.detail}" if self.detail else ""
        return f"{head}{context}{extra}"


#: Kept so existing call sites and tests keep working after the catalog stopped
#: being OpenRouter-only.
OpenRouterModel = CatalogModel


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Safe accounting for one model invocation in an execution trace."""

    provider: str
    model: str
    role: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    latency_ms: float = 0.0
    success: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ExecutionTraceStep:
    """One presentation-neutral, deliberately redacted execution-map node."""

    id: str
    kind: str
    title: str
    detail: str
    status: str
    timestamp: datetime
    target: str = ""
    duration_seconds: float = 0.0
    model_usage: ModelUsage | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPrompt:
    """A mission-backed user prompt shown in the execution-map index."""

    mission_id: str
    request: str
    status: str
    created_at: datetime
    total_tokens: int = 0
    estimated_cost: float = 0.0
    step_count: int = 0
    tool_count: int = 0
    model_call_count: int = 0

    @property
    def title(self) -> str:
        compact = " ".join(self.request.split())
        return compact[:117] + "…" if len(compact) > 120 else compact


@dataclass(frozen=True, slots=True)
class ExecutionTrace:
    """The ordered, safe audit graph for one prompt/mission."""

    mission_id: str
    request: str
    status: str
    created_at: datetime
    steps: tuple[ExecutionTraceStep, ...] = ()
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost: float = 0.0
    total_model_latency_ms: float = 0.0
    total_tool_duration_seconds: float = 0.0
    model_call_count: int = 0
    tool_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens
