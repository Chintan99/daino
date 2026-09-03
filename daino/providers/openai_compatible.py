"""OpenAI-compatible provider used by OpenRouter, Ollama, vLLM, and private gateways."""

from __future__ import annotations

import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from daino.exceptions import (
    ProviderError,
    StructuredConstraintUnsupported,
    ToolCallingUnsupported,
)
from daino.providers.base import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider, ProviderUsage
from daino.providers.gate import request_slot
from daino.schemas import LLMResponse, Message, ToolCall

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
    if message.images and message.role == "user":
        # The multi-part content form. Only used when there is actually an image:
        # a plain string is what every backend accepts, including the local ones
        # whose OpenAI compatibility stops at the simple shape.
        wire["content"] = [
            *([{"type": "text", "text": message.content}] if message.content else []),
            *(
                {"type": "image_url", "image_url": {"url": image.data_url}}
                for image in message.images
            ),
        ]
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
    if message.role == "tool":
        if message.tool_call_id:
            wire["tool_call_id"] = message.tool_call_id
        else:
            # A prompted (JSON-action) observation has no native tool call to
            # reference. OpenAI-compatible APIs reject a `tool` message without a
            # `tool_call_id` (OpenRouter answers HTTP 400 "missing field
            # tool_call_id"), so deliver the observation as a user turn instead —
            # the model still sees it, and the request stays valid.
            wire["role"] = "user"
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


_REASONING_FIELDS = ("reasoning", "reasoning_content", "thinking")


def _text_fragment(value: object) -> str:
    """Normalize text-shaped compatibility fields without stringifying objects."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key in ("text", "content", "value"):
                text = item.get(key)
                if isinstance(text, str):
                    parts.append(text)
                    break
    return "".join(parts)


def _reasoning_fragment(message: object) -> str:
    """Read the common reasoning aliases used by OpenAI-compatible servers."""
    if not isinstance(message, dict):
        return ""
    for field_name in _REASONING_FIELDS:
        value = _text_fragment(message.get(field_name))
        if value:
            return value
    return ""


def _choice_message(choice: object) -> dict[str, Any]:
    """Return either a streamed delta or a non-streaming message object."""
    if not isinstance(choice, dict):
        return {}
    delta = choice.get("delta")
    if isinstance(delta, dict):
        return delta
    message = choice.get("message")
    return message if isinstance(message, dict) else {}


def _merge_fragment(current: str, fragment: str) -> str:
    """Join wire fragments while tolerating servers that repeat the full value."""
    if not fragment:
        return current
    if not current or fragment.startswith(current):
        return fragment
    if fragment == current:
        return current
    return current + fragment


def _error_text(exc: Exception | None) -> str:
    """Describe a transport failure even when the exception carries no message.

    ``httpx`` timeout exceptions stringify to the empty string, so a local model
    that simply took too long produced "request failed after retries: " and left
    the user with nothing to act on. Naming the class, and the timeout in
    particular, makes the cause legible.
    """
    if exc is None:
        return "no response from the provider"
    detail = str(exc).strip()
    name = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        hint = (
            "the model did not respond in time; raise the provider timeout or use a smaller model"
        )
        return f"{name}: {detail or hint}"
    return f"{name}: {detail}" if detail else name


def _add_usage(left: ProviderUsage, right: ProviderUsage) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cost=left.cost + right.cost,
        cached_tokens=left.cached_tokens + right.cached_tokens,
    )


@dataclass(slots=True)
class _StreamAccumulator:
    """Reconstruct one Chat Completions response from indexed SSE deltas."""

    default_model: str
    model: str = ""
    content: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    saw_choice: bool = False

    def consume(self, chunk: dict[str, Any], usage: ProviderUsage) -> dict[str, Any]:
        self.usage = _add_usage(self.usage, usage)
        wire_model = chunk.get("model")
        if isinstance(wire_model, str) and wire_model:
            self.model = wire_model
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return {}
        choice = choices[0]
        if not isinstance(choice, dict):
            return {}
        self.saw_choice = True
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            self.finish_reason = str(finish_reason)
        message = _choice_message(choice)
        content = message.get("content")
        if isinstance(content, str) and content:
            self.content.append(content)
        self._consume_tool_calls(message.get("tool_calls"))
        return message

    def _consume_tool_calls(self, raw_calls: object) -> None:
        if not isinstance(raw_calls, list):
            return
        for position, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue
            raw_index = raw_call.get("index", position)
            try:
                index = max(0, int(raw_index))
            except (TypeError, ValueError):
                index = position
            call = self.tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            identifier = raw_call.get("id")
            if isinstance(identifier, str) and identifier:
                call["id"] = identifier
            call_type = raw_call.get("type")
            if isinstance(call_type, str) and call_type:
                call["type"] = call_type
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            stored_function = call["function"]
            name = function.get("name")
            if isinstance(name, str):
                stored_function["name"] = _merge_fragment(stored_function["name"], name)
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                stored_function["arguments"] = arguments
            elif isinstance(arguments, str):
                current = stored_function["arguments"]
                if not isinstance(current, str):
                    current = ""
                stored_function["arguments"] = _merge_fragment(current, arguments)

    def response(self) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        for index, raw_call in sorted(self.tool_calls.items()):
            call = dict(raw_call)
            call["id"] = call.get("id") or f"call_{index + 1}"
            calls.append(call)
        return {
            "model": self.model or self.default_model,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(self.content),
                        "tool_calls": calls,
                    },
                    "finish_reason": self.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": self.usage.input_tokens,
                "completion_tokens": self.usage.output_tokens,
                "cost": self.usage.cost,
                "prompt_tokens_details": {"cached_tokens": self.usage.cached_tokens},
            },
        }


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Decode SSE data blocks, ignoring comments and unrelated fields."""
    parts: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if parts:
                yield "\n".join(parts)
                parts.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            parts.append(value)
    if parts:
        yield "\n".join(parts)


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
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 0,
    ) -> None:
        self.name = name
        self.model = model
        #: In-flight generation requests allowed against this provider; 0 is
        #: unlimited.
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.features = set(features or ["chat", "structured"])
        self.reasoning_effort = reasoning_effort
        self._last_usage = ProviderUsage()
        self.set_reasoning_handler(None)
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        #: The gate is keyed by endpoint, not by provider name: the thing that
        #: can only serve one request at a time is the model server. Two config
        #: entries pointing at the same Ollama share its queue; two Ollamas on
        #: different hosts must not block each other. (Adapters also hardcode
        #: their own ``name``, so a name key would lump every Ollama together.)
        self._gate_key = base_url.rstrip("/").casefold()
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one completion request, holding this provider's request slot.

        The slot is taken *before* the HTTP call, so the client timeout starts
        when the request is actually sent rather than while it queues — waiting
        inside the model server was what turned a slow fan-out into a timeout.
        """
        async with request_slot(self._gate_key, self.concurrency):
            return await self._post_now(payload)

    async def _post_now(self, payload: dict[str, Any]) -> dict[str, Any]:
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
                    raise self._rejected_request(exc, payload) from exc
                if attempt >= self.max_retries:
                    break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
        raise ProviderError(f"{self.name} request failed after retries: {_error_text(last_error)}")

    def _rejected_request(
        self, exc: httpx.HTTPStatusError, payload: dict[str, Any]
    ) -> ProviderError:
        """Classify terminal 4xx responses identically for JSON and SSE calls."""
        status = exc.response.status_code
        error_type: type[ProviderError] = ProviderError
        # Only request-shape status codes mean a feature is unsupported.
        # Authentication and quota failures must not be retried without
        # tools/schema, which only duplicates a doomed (and billable) request.
        if status in {400, 404, 405, 415, 422}:
            if payload.get("tools"):
                error_type = ToolCallingUnsupported
            elif any(name in payload for name in ("response_format", "format", "guided_json")):
                error_type = StructuredConstraintUnsupported
        return error_type(
            f"{self.name} rejected the request (HTTP {status}): {exc.response.text[:300]}"
        )

    async def _stream_json(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded Chat Completions events with bounded retry behavior.

        Some compatibility gateways return a regular JSON completion even when
        ``stream`` is requested.  Accept that response as a single event so
        reasoning remains observable, albeit only once that server finishes.
        """
        wire_payload = dict(payload)
        wire_payload["stream"] = True
        stream_options = wire_payload.get("stream_options")
        wire_payload["stream_options"] = {
            **(stream_options if isinstance(stream_options, dict) else {}),
            "include_usage": True,
        }
        last_error: Exception | None = None
        attempt = 0
        stream_options_fallback = True
        while attempt <= self.max_retries:
            received = False
            try:
                async with self.client.stream(
                    "POST", "chat/completions", json=wire_payload
                ) as response:
                    # Streaming responses are not read automatically. Read an
                    # error body before classification so response.text is safe.
                    if response.is_error:
                        await response.aread()
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    if "application/json" in content_type:
                        await response.aread()
                        decoded = response.json()
                        if not isinstance(decoded, dict):
                            raise ValueError("response JSON is not an object")
                        received = True
                        yield cast(dict[str, Any], decoded)
                        return
                    async for data in _sse_data(response):
                        if data.strip() == "[DONE]":
                            return
                        decoded = json.loads(data)
                        if not isinstance(decoded, dict):
                            raise ValueError("stream event JSON is not an object")
                        received = True
                        yield cast(dict[str, Any], decoded)
                    return
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if (
                    stream_options_fallback
                    and status in {400, 404, 405, 415, 422}
                    and "stream_options" in wire_payload
                ):
                    # Older compatibility servers support streaming but reject
                    # OpenAI's usage option. Retry this exact call once without
                    # it before attributing the rejection to tools or schema.
                    wire_payload.pop("stream_options", None)
                    stream_options_fallback = False
                    continue
                if status != 429 and status < 500:
                    raise self._rejected_request(exc, wire_payload) from exc
                if received or attempt >= self.max_retries:
                    break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                # Once a fragment was shown, retrying would duplicate the start
                # of the model's reasoning or answer in the observer.
                if received or attempt >= self.max_retries:
                    break
            attempt += 1
        raise ProviderError(
            f"{self.name} streaming request failed after retries: {_error_text(last_error)}"
        )

    async def _streamed_response(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], ProviderUsage]:
        """Collect SSE into the same response shape consumed by ``complete``."""
        accumulator = _StreamAccumulator(self.model)
        async with request_slot(self._gate_key, self.concurrency):
            async for chunk in self._stream_json(payload):
                usage = self._capture_usage(chunk)
                message = accumulator.consume(chunk, usage)
                self._emit_reasoning(_reasoning_fragment(message))
        if not accumulator.saw_choice:
            raise ProviderError(f"Malformed streaming response from {self.name}: no choices")
        return accumulator.response(), accumulator.usage

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
        self._apply_reasoning_effort(payload)
        started = time.monotonic()
        if self._has_reasoning_handler():
            data, usage = await self._streamed_response(payload)
        else:
            data = await self._post(payload)
            usage = self._capture_usage(data)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            # This is normally a no-op because installing a handler selects the
            # live SSE path. Keep it for gateways that ignore stream=true and
            # return a conventional JSON response.
            self._emit_reasoning(_reasoning_fragment(message))
            return LLMResponse(
                content=message.get("content") or "",
                model=data.get("model", self.model),
                provider=self.name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
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
        }
        self._apply_reasoning_effort(payload)
        try:
            async for chunk in self._stream_json(payload):
                self._capture_usage(chunk)
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                message = _choice_message(choices[0])
                self._emit_reasoning(_reasoning_fragment(message))
                content = message.get("content")
                if isinstance(content, str) and content:
                    yield content
        except ProviderError:
            raise
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
            self._apply_reasoning_effort(payload)
            if constrained:
                self._constrain_payload(payload, schema_json, schema.__name__)
            try:
                if self._has_reasoning_handler():
                    data, _ = await self._streamed_response(payload)
                else:
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
                message = data["choices"][0]["message"]
                self._emit_reasoning(_reasoning_fragment(message))
                text = message["content"]
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

    def _apply_reasoning_effort(self, payload: dict[str, Any]) -> None:
        """Apply the OpenAI-compatible reasoning control when explicitly selected."""
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

    def _structured_failure(self, exc: Exception, truncated: bool) -> str:
        if truncated:
            return (
                f"{self.name} stopped at the {self.max_output_tokens}-token output limit "
                "before finishing its JSON, so the reply could not be parsed. The model was "
                "most likely rewriting a whole file. Ask for a smaller, targeted change, or "
                f"raise max_output_tokens for the {self.name} provider in .daino/config.yaml."
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

    def reset_usage(self) -> None:
        self._last_usage = ProviderUsage()

    def _capture_usage(self, data: dict[str, Any]) -> ProviderUsage:
        """Accumulate usage from a response or final streaming chunk."""
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            return ProviderUsage()

        usage = ProviderUsage(
            input_tokens=self._safe_nonnegative_int(raw_usage.get("prompt_tokens")),
            output_tokens=self._safe_nonnegative_int(raw_usage.get("completion_tokens")),
            cost=self._safe_nonnegative_float(raw_usage.get("cost")),
            cached_tokens=self._cached_tokens(raw_usage),
        )
        previous = self._last_usage
        self._last_usage = _add_usage(previous, usage)
        return usage

    def _cached_tokens(self, raw_usage: dict[str, Any]) -> int:
        """Prompt tokens the provider served from cache, if it says so.

        Worth having even though nothing acts on it yet: a cached prefix costs a
        fraction of a fresh one, so without this number there is no way to tell a
        turn that reused its prefix from one that paid for it 124 times over.
        """
        details = raw_usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            return self._safe_nonnegative_int(details.get("cached_tokens"))
        # Some gateways flatten it. Read that spelling too rather than reporting
        # a zero that reads as "no cache".
        return self._safe_nonnegative_int(raw_usage.get("cached_tokens"))

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
