"""Provider-independent asynchronous LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from daino.schemas import LLMResponse, Message

StructuredT = TypeVar("StructuredT", bound=BaseModel)
ReasoningHandler = Callable[[str], None]

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
    #: How much of ``input_tokens`` the provider served from its prompt cache.
    #: Reported by OpenAI-compatible endpoints as
    #: ``usage.prompt_tokens_details.cached_tokens``. Zero means either no cache
    #: hit or a provider that does not say — the two are indistinguishable on the
    #: wire, which is why the number is recorded rather than inferred.
    cached_tokens: int = 0


class LLMProvider(ABC):
    """Contract implemented by every model backend."""

    name: str
    model: str

    def set_reasoning_handler(self, handler: ReasoningHandler | None) -> None:
        """Receive provider-supplied reasoning text as it arrives.

        The callback is deliberately separate from :meth:`stream`: reasoning
        must never be mixed into the assistant's answer.  It is optional so
        existing provider adapters and callers retain their previous behavior.
        """
        self._reasoning_handler = handler

    def _emit_reasoning(self, content: str) -> None:
        """Forward a non-empty reasoning fragment to the optional observer."""
        handler = getattr(self, "_reasoning_handler", None)
        if handler is not None and content:
            handler(content)

    def _has_reasoning_handler(self) -> bool:
        return getattr(self, "_reasoning_handler", None) is not None

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
