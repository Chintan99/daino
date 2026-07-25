"""OpenAI-compatible provider used by OpenRouter, vLLM, and private gateways."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from vasuki.exceptions import ProviderError
from vasuki.providers.base import LLMProvider
from vasuki.schemas import LLMResponse, Message

StructuredT = TypeVar("StructuredT", bound=BaseModel)


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1])
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


class OpenAICompatibleProvider(LLMProvider):
    """Minimal, retrying async client for the stable chat-completions protocol."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120,
        max_retries: int = 2,
        max_output_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
        features: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.features = set(features or ["chat", "structured"])
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.post("chat/completions", json=payload)
                response.raise_for_status()
                return cast(dict[str, Any], response.json())
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
        raise ProviderError(f"{self.name} request failed after retries: {last_error}")

    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_output_tokens,
        }
        started = time.monotonic()
        data = await self._post(payload)
        try:
            choice = data["choices"][0]
            usage = data.get("usage", {})
            return LLMResponse(
                content=choice["message"]["content"] or "",
                model=data.get("model", self.model),
                provider=self.name,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                latency_ms=(time.monotonic() - started) * 1000,
                finish_reason=choice.get("finish_reason"),
                raw=data,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Malformed response from {self.name}") from exc

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "stream": True,
        }
        try:
            async with self.client.stream("POST", "chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line.removeprefix("data: "))
                    content = chunk["choices"][0].get("delta", {}).get("content")
                    if content:
                        yield content
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise ProviderError(f"Streaming request failed: {exc}") from exc

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        max_repair_attempts: int = 2,
    ) -> StructuredT:
        schema_instruction = "\nReturn only JSON matching this JSON Schema:\n" + json.dumps(
            schema.model_json_schema(), separators=(",", ":")
        )
        current = [*messages]
        if current:
            current[-1] = Message(
                role=current[-1].role,
                content=current[-1].content + schema_instruction,
            )
        for attempt in range(max_repair_attempts + 1):
            payload = {
                "model": self.model,
                "messages": [message.model_dump() for message in current],
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
            }
            if self.supports_json_schema():
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": schema.model_json_schema(),
                    },
                }
            data = await self._post(payload)
            try:
                text = data["choices"][0]["message"]["content"]
                return schema.model_validate(_extract_json(text))
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                if attempt >= max_repair_attempts:
                    raise ProviderError(f"Invalid structured response: {exc}") from exc
                current.extend(
                    [
                        Message(role="assistant", content=str(data)),
                        Message(
                            role="user",
                            content=(
                                f"Your response failed schema validation: {exc}. "
                                "Return corrected JSON only."
                            ),
                        ),
                    ]
                )
        raise ProviderError("Structured response retry loop exhausted")

    def supports_tools(self) -> bool:
        return "tools" in self.features

    def supports_json_schema(self) -> bool:
        return "structured" in self.features or "json_schema" in self.features

    async def health_check(self) -> dict[str, object]:
        started = time.monotonic()
        try:
            response = await self.client.get("models")
            response.raise_for_status()
            return {
                "healthy": True,
                "latency_ms": (time.monotonic() - started) * 1000,
                "model": self.model,
            }
        except httpx.HTTPError as exc:
            return {"healthy": False, "error": str(exc), "model": self.model}

    async def close(self) -> None:
        await self.client.aclose()
