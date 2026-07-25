"""Provider-independent asynchronous LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TypeVar

from pydantic import BaseModel

from vasuki.schemas import LLMResponse, Message

StructuredT = TypeVar("StructuredT", bound=BaseModel)


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
