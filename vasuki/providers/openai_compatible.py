"""OpenAI-compatible provider used by OpenRouter, Ollama, vLLM, and private gateways."""

from __future__ import annotations

import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from vasuki.exceptions import (
    ProviderError,
    StructuredConstraintUnsupported,
    ToolCallingUnsupported,
)
from vasuki.providers.base import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider, ProviderUsage
from vasuki.schemas import LLMResponse, Message, ToolCall

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


def _wire_message(message: Message) -> dict[str, Any]:
    """Translate an internal message into the OpenAI chat-completions wire format."""
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in message.tool_calls
        ]
        if not message.content:
            wire["content"] = None
    if message.role == "tool" and message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _hit_token_limit(data: dict[str, Any]) -> bool:
    """Report whether the model was cut off at the output-token ceiling.

    A truncated reply is not malformed output the model can be asked to correct:
    it ran out of room. Telling the two apart is the difference between a useful
    error and "Unterminated string at line 5".
    """
    try:
        return str(data["choices"][0].get("finish_reason") or "") == "length"
    except (KeyError, IndexError, TypeError):
        return False


def _repair_instruction(exc: Exception, truncated: bool) -> str:
    if truncated:
        return (
            "Your previous reply was cut off at the output token limit before the JSON "
            "closed, because it was too long. Do not resend it. Make one smaller change "
            "instead: use the replace action with a short old_string and new_string that "
            "touch only the lines that must change, rather than rewriting the whole file."
        )
    return (
        f"Your response failed schema validation: {exc}. Return corrected JSON only, "
        "with every string properly escaped and closed."
    )


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    """Parse OpenAI-format tool calls, tolerating malformed individual entries."""
    calls: list[ToolCall] = []
    if not isinstance(raw_calls, list):
        return calls
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                parsed = json.loads(str(raw_arguments))
                arguments = parsed if isinstance(parsed, dict) else {"raw_arguments": raw_arguments}
            except json.JSONDecodeError:
                arguments = {"raw_arguments": raw_arguments}
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{len(calls) + 1}"),
                name=name,
                arguments=arguments,
            )
        )
    return calls


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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        extra_headers: dict[str, str] | None = None,
        features: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.features = set(features or ["chat", "structured"])
        self._last_usage = ProviderUsage()
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
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                # A 4xx (other than rate limiting) is a rejection of the request
                # itself; retrying an identical payload cannot succeed.
                if status != 429 and status < 500:
                    error_type: type[ProviderError] = ProviderError
                    # Only request-shape status codes mean a feature is
                    # unsupported. Authentication and quota failures must not
                    # be retried without tools/schema, which only duplicates a
                    # doomed (and potentially billable) request.
                    if status in {400, 404, 405, 415, 422}:
                        if payload.get("tools"):
                            error_type = ToolCallingUnsupported
                        elif any(
                            name in payload for name in ("response_format", "format", "guided_json")
                        ):
                            error_type = StructuredConstraintUnsupported
                    raise error_type(
                        f"{self.name} rejected the request (HTTP {status}): "
                        f"{exc.response.text[:300]}"
                    ) from exc
                if attempt >= self.max_retries:
                    break
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
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_wire_message(message) for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        started = time.monotonic()
        data = await self._post(payload)
        usage = self._capture_usage(data)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            return LLMResponse(
                content=message.get("content") or "",
                model=data.get("model", self.model),
                provider=self.name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=(time.monotonic() - started) * 1000,
                finish_reason=choice.get("finish_reason"),
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
                raw=data,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Malformed response from {self.name}") from exc

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [_wire_message(message) for message in messages],
            "stream": True,
        }
        try:
            async with self.client.stream("POST", "chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line.removeprefix("data: "))
                    self._capture_usage(chunk)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content")
                    if content:
                        yield content
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise ProviderError(f"Streaming request failed: {exc}") from exc

    def _constrain_payload(
        self, payload: dict[str, Any], schema_json: dict[str, Any], schema_name: str
    ) -> None:
        """Apply this backend's grammar-constrained decoding parameter.

        Subclasses override this to use the constraint mechanism their server
        actually implements: OpenAI-style ``response_format`` here, ``format``
        on Ollama, ``guided_json`` on vLLM. If the server rejects the parameter
        the caller retries once without it, so newer and older server versions
        both keep working.
        """
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema_json,
            },
        }

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        max_repair_attempts: int = 2,
    ) -> StructuredT:
        schema_json = schema.model_json_schema()
        schema_instruction = "\nReturn only JSON matching this JSON Schema:\n" + json.dumps(
            schema_json, separators=(",", ":")
        )
        current = [*messages]
        if current:
            current[-1] = Message(
                role=current[-1].role,
                content=current[-1].content + schema_instruction,
            )
        constrained = self.supports_json_schema()
        attempt = 0
        while attempt <= max_repair_attempts:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [_wire_message(message) for message in current],
                "temperature": 0,
                "max_tokens": self.max_output_tokens,
            }
            if constrained:
                self._constrain_payload(payload, schema_json, schema.__name__)
            try:
                data = await self._post(payload)
                self._capture_usage(data)
            except StructuredConstraintUnsupported:
                if constrained:
                    # The server does not accept this backend's constraint
                    # parameter; fall back to prompt-only JSON once.
                    constrained = False
                    continue
                raise
            truncated = _hit_token_limit(data)
            try:
                text = data["choices"][0]["message"]["content"]
                return schema.model_validate(_extract_json(text))
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                if attempt >= max_repair_attempts:
                    raise ProviderError(self._structured_failure(exc, truncated)) from exc
                # Feeding a truncated reply back asks the model to repeat work it
                # already could not fit, and the reply itself eats the budget that
                # made it too small. Ask for a smaller edit instead.
                current.append(Message(role="user", content=_repair_instruction(exc, truncated)))
            attempt += 1
        raise ProviderError("Structured response retry loop exhausted")

    def _structured_failure(self, exc: Exception, truncated: bool) -> str:
        if truncated:
            return (
                f"{self.name} stopped at the {self.max_output_tokens}-token output limit "
                "before finishing its JSON, so the reply could not be parsed. The model was "
                "most likely rewriting a whole file. Ask for a smaller, targeted change, or "
                f"raise max_output_tokens for the {self.name} provider in .vasuki/config.yaml."
            )
        return f"Invalid structured response: {exc}"

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

    @property
    def last_usage(self) -> ProviderUsage:
        """Return token counts and provider-reported cost for this call."""
        return self._last_usage

    def _capture_usage(self, data: dict[str, Any]) -> ProviderUsage:
        """Accumulate usage from a response or final streaming chunk."""
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            return ProviderUsage()

        usage = ProviderUsage(
            input_tokens=self._safe_nonnegative_int(raw_usage.get("prompt_tokens")),
            output_tokens=self._safe_nonnegative_int(raw_usage.get("completion_tokens")),
            cost=self._safe_nonnegative_float(raw_usage.get("cost")),
        )
        previous = self._last_usage
        self._last_usage = ProviderUsage(
            input_tokens=previous.input_tokens + usage.input_tokens,
            output_tokens=previous.output_tokens + usage.output_tokens,
            cost=previous.cost + usage.cost,
        )
        return usage

    @staticmethod
    def _safe_nonnegative_int(value: object) -> int:
        if not isinstance(value, (str, int, float)):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_nonnegative_float(value: object) -> float:
        if not isinstance(value, (str, int, float)):
            return 0.0
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(parsed):
            return 0.0
        return max(0.0, parsed)
