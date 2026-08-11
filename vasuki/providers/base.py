"""Provider-independent asynchronous LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from vasuki.schemas import LLMResponse, Message

StructuredT = TypeVar("StructuredT", bound=BaseModel)

#: Output ceiling for one model reply. A coding agent writes files, so the old
#: 4096 was routinely hit mid-JSON: the reply came back truncated and unparseable
#: rather than merely short. Large enough for a real file, still bounded.
DEFAULT_MAX_OUTPUT_TOKENS = 16_384


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Usage reported by a provider for the current request."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


class LLMProvider(ABC):
    """Contract implemented by every model backend."""

    name: str
    model: str

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        yield ""

    @abstractmethod
    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        max_repair_attempts: int = 2,
    ) -> StructuredT: ...

    @abstractmethod
    def supports_tools(self) -> bool: ...

    @abstractmethod
    def supports_json_schema(self) -> bool: ...

    @abstractmethod
    async def health_check(self) -> dict[str, object]: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    def last_usage(self) -> ProviderUsage:
        """Return usage accumulated during the current provider call."""
        return ProviderUsage()
