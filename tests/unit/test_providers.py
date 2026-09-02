from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from daino.config.models import ProviderConfig
from daino.exceptions import ProviderError, ToolCallingUnsupported
from daino.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
from daino.providers.factory import create_provider
from daino.providers.ollama import OllamaProvider
from daino.providers.openai_compatible import OpenAICompatibleProvider
from daino.providers.openrouter import OpenRouterProvider
from daino.providers.vllm import VLLMProvider
from daino.schemas import Message, ToolCall


class Answer(BaseModel):
    value: int


def response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "mock",
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    )


def stream_response(*events: dict[str, Any]) -> httpx.Response:
    body = "\n\n".join(
        [*(f"data: {json.dumps(event)}" for event in events), "data: [DONE]", ""]
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
async def test_openai_compatible_complete_and_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return response("hello")

    provider = OpenAICompatibleProvider(
        name="mock",
        base_url="https://mock.invalid/v1",
        api_key="test",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete([Message(role="user", content="hello")])
    health = await provider.health_check()
    await provider.close()
    assert result.content == "hello"
    assert result.input_tokens == 4
    assert health["healthy"] is True


@pytest.mark.asyncio
async def test_structured_output_repairs_invalid_json() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response("not json" if calls == 1 else json.dumps({"value": 42}))

    provider = OpenAICompatibleProvider(
        name="mock",
        base_url="https://mock.invalid/v1",
        api_key="",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.structured_complete([Message(role="user", content="answer")], Answer)
    await provider.close()
    assert result.value == 42
    assert calls == 2


@pytest.mark.asyncio
async def test_openrouter_headers_and_vllm_empty_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response("ok")

    transport = httpx.MockTransport(handler)
    openrouter = OpenRouterProvider(
        api_key="secret",
        model="mock",
        application_name="Daino",
        referring_url="https://example.invalid",
        transport=transport,
    )
    await openrouter.complete([Message(role="user", content="test")])
    await openrouter.close()
    vllm = VLLMProvider(model="mock", api_key="", transport=transport)
    await vllm.complete([Message(role="user", content="test")])
    await vllm.close()
    assert seen[0].headers["x-title"] == "Daino"
    assert seen[0].headers["http-referer"] == "https://example.invalid"
    assert "authorization" not in seen[1].headers


@pytest.mark.asyncio
async def test_reasoning_effort_uses_provider_specific_wire_format() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response("ok")

    transport = httpx.MockTransport(handler)
    compatible = OpenAICompatibleProvider(
        name="compatible",
        base_url="https://compatible.invalid/v1",
        api_key="",
        model="reasoning-model",
        reasoning_effort="high",
        transport=transport,
    )
    openrouter = OpenRouterProvider(
        api_key="secret",
        model="openai/reasoning-model",
        reasoning_effort="low",
        transport=transport,
    )
    ollama = OllamaProvider(
        model="qwen3",
        reasoning_effort="medium",
        transport=transport,
    )

    await compatible.complete([Message(role="user", content="test")])
    await openrouter.complete([Message(role="user", content="test")])
    await ollama.complete([Message(role="user", content="test")])
    await compatible.close()
    await openrouter.close()
    await ollama.close()

    assert seen[0]["reasoning_effort"] == "high"
    assert "reasoning" not in seen[0]
    assert seen[1]["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in seen[1]
    assert seen[2]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_openrouter_captures_provider_reported_cost() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        result = response("ok")
        payload = json.loads(result.content)
        payload["usage"]["cost"] = 0.01234
        return httpx.Response(200, json=payload)

    provider = OpenRouterProvider(
        api_key="secret",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    await provider.complete([Message(role="user", content="test")])

    assert provider.last_usage.input_tokens == 4
    assert provider.last_usage.output_tokens == 2
    assert provider.last_usage.cost == pytest.approx(0.01234)
    await provider.close()


@pytest.mark.asyncio
async def test_openrouter_captures_usage_from_final_stream_chunk() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        stream = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"content":"hello"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":9,'
                '"completion_tokens":3,"cost":0.0042}}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    provider = OpenRouterProvider(
        api_key="secret",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="test")])]

    assert chunks == ["hello"]
    assert provider.last_usage.input_tokens == 9
    assert provider.last_usage.output_tokens == 3
    assert provider.last_usage.cost == pytest.approx(0.0042)
    await provider.close()


@pytest.mark.asyncio
async def test_prompt_cache_hits_are_captured_so_they_can_be_seen() -> None:
    """A turn that reuses its prefix and one that pays for it look identical
    without this number, and they bill very differently."""

    def handler(_: httpx.Request) -> httpx.Response:
        stream = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"content":"hi"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":40000,'
                '"completion_tokens":120,"prompt_tokens_details":{"cached_tokens":36000}}}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    provider = OpenRouterProvider(
        api_key="secret", model="mock", transport=httpx.MockTransport(handler)
    )
    [chunk async for chunk in provider.stream([Message(role="user", content="test")])]

    assert provider.last_usage.input_tokens == 40_000
    assert provider.last_usage.cached_tokens == 36_000
    await provider.close()


@pytest.mark.asyncio
async def test_a_provider_that_reports_no_cache_detail_reads_as_zero() -> None:
    """Absent is not an error — most gateways simply do not say."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    provider = OpenRouterProvider(
        api_key="secret", model="mock", transport=httpx.MockTransport(handler)
    )
    response = await provider.complete([Message(role="user", content="test")])

    assert response.input_tokens == 10
    assert response.cached_tokens == 0
    await provider.close()


@pytest.mark.asyncio
async def test_a_flattened_cache_field_is_read_too() -> None:
    """Some gateways report it beside the totals rather than nested."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 2, "cached_tokens": 80},
            },
        )

    provider = OpenRouterProvider(
        api_key="secret", model="mock", transport=httpx.MockTransport(handler)
    )
    response = await provider.complete([Message(role="user", content="test")])

    assert response.cached_tokens == 80
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_stream_forwards_delta_reasoning_but_yields_only_answer() -> None:
    seen: list[dict[str, Any]] = []
    reasoning: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return stream_response(
            {
                "model": "qwen3.8:27b-mlx",
                "choices": [
                    {"index": 0, "delta": {"content": "", "reasoning": "Inspect"}}
                ],
            },
            {
                "model": "qwen3.8:27b-mlx",
                "choices": [
                    {"index": 0, "delta": {"content": "", "reasoning": " files"}}
                ],
            },
            {
                "model": "qwen3.8:27b-mlx",
                "choices": [{"index": 0, "delta": {"content": "Done"}}],
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3, "cost": 0.0},
            },
        )

    provider = OllamaProvider(
        model="qwen3.8:27b-mlx", transport=httpx.MockTransport(handler)
    )
    provider.set_reasoning_handler(reasoning.append)
    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="x")])]
    await provider.close()

    assert chunks == ["Done"]
    assert reasoning == ["Inspect", " files"]
    assert seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert provider.last_usage == provider.last_usage.__class__(
        input_tokens=9, output_tokens=3, cost=0.0
    )


@pytest.mark.asyncio
async def test_complete_streams_reasoning_and_reconstructs_fragmented_tool_calls() -> None:
    reasoning: list[str] = []
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return stream_response(
            {
                "model": "qwen3.8:27b-mlx",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning": "I should inspect app.py.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_7",
                                    "type": "function",
                                    "function": {
                                        "name": "read_",
                                        "arguments": '{"path":"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "file", "arguments": 'app.py"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "cost": 0.002},
            },
        )

    provider = OllamaProvider(
        model="qwen3.8:27b-mlx", transport=httpx.MockTransport(handler)
    )
    provider.set_reasoning_handler(reasoning.append)
    result = await provider.complete(
        [Message(role="user", content="inspect")],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        tool_choice="required",
    )
    await provider.close()

    assert seen[0]["stream"] is True
    assert reasoning == ["I should inspect app.py."]
    assert result.finish_reason == "tool_calls"
    assert result.model == "qwen3.8:27b-mlx"
    assert result.tool_calls == [
        ToolCall(id="call_7", name="read_file", arguments={"path": "app.py"})
    ]
    assert (result.input_tokens, result.output_tokens) == (12, 7)
    assert provider.last_usage.cost == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_structured_completion_streams_reasoning_and_json_fragments() -> None:
    reasoning: list[str] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return stream_response(
            {
                "model": "qwen3",
                "choices": [
                    {"index": 0, "delta": {"thinking": "Check the schema. "}}
                ],
            },
            {
                "model": "qwen3",
                "choices": [{"index": 0, "delta": {"content": '{"val'}}],
            },
            {
                "model": "qwen3",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "Return an integer."},
                    }
                ],
            },
            {
                "model": "qwen3",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": 'ue":42}'},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
        )

    provider = OllamaProvider(model="qwen3", transport=httpx.MockTransport(handler))
    provider.set_reasoning_handler(reasoning.append)
    result = await provider.structured_complete(
        [Message(role="user", content="answer")], Answer
    )
    await provider.close()

    assert result.value == 42
    assert reasoning == ["Check the schema. ", "Return an integer."]
    assert provider.last_usage.input_tokens == 20
    assert provider.last_usage.output_tokens == 8


@pytest.mark.parametrize("field_name", ["reasoning", "reasoning_content", "thinking"])
@pytest.mark.asyncio
async def test_json_fallback_forwards_nonstream_reasoning_alias_once(field_name: str) -> None:
    seen: list[dict[str, Any]] = []
    reasoning: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        result = response("done")
        payload = json.loads(result.content)
        payload["choices"][0]["message"][field_name] = "provider reasoning"
        return httpx.Response(200, json=payload)

    provider = OpenAICompatibleProvider(
        name="compatible",
        base_url="https://compatible.invalid/v1",
        api_key="",
        model="reasoning-model",
        transport=httpx.MockTransport(handler),
    )
    provider.set_reasoning_handler(reasoning.append)
    result = await provider.complete([Message(role="user", content="test")])
    await provider.close()

    assert seen[0]["stream"] is True
    assert result.content == "done"
    assert reasoning == ["provider reasoning"]


@pytest.mark.asyncio
async def test_reasoning_stream_retries_once_without_unsupported_usage_option() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if "stream_options" in payload:
            return httpx.Response(400, json={"error": "unknown field stream_options"})
        return stream_response(
            {
                "model": "legacy",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning": "thinking", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    provider = OpenAICompatibleProvider(
        name="legacy",
        base_url="https://legacy.invalid/v1",
        api_key="",
        model="legacy",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    reasoning: list[str] = []
    provider.set_reasoning_handler(reasoning.append)
    result = await provider.complete([Message(role="user", content="x")])
    await provider.close()

    assert len(seen) == 2
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in seen[1]
    assert result.content == "done"
    assert reasoning == ["thinking"]


@pytest.mark.asyncio
async def test_openrouter_validates_key_and_lists_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(
                200,
                json={"data": {"label": "sk-or-v1-test...123", "limit_remaining": 10}},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/model-b",
                        "name": "Model B",
                        "context_length": 128_000,
                    },
                    {
                        "id": "anthropic/model-a",
                        "name": "Model A",
                        "context_length": 200_000,
                    },
                ]
            },
        )

    provider = OpenRouterProvider(
        api_key="valid",
        model="anthropic/model-a",
        transport=httpx.MockTransport(handler),
    )
    details = await provider.validate_key()
    models = await provider.list_models()
    await provider.close()

    assert details["limit_remaining"] == 10
    assert [item["id"] for item in models] == [
        "openai/model-b",
        "anthropic/model-a",
    ]


@pytest.mark.asyncio
async def test_openrouter_invalid_key_includes_safe_reason() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "User not found."}},
        )

    provider = OpenRouterProvider(
        api_key="invalid",
        model="openrouter/auto",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match=r"HTTP 401.*User not found"):
        await provider.validate_key()
    await provider.close()


def _capture(handler_state: list[dict[str, object]]):
    def handler(request: httpx.Request) -> httpx.Response:
        handler_state.append(json.loads(request.content))
        return response(json.dumps({"value": 7}))

    return handler


@pytest.mark.asyncio
async def test_ollama_structured_uses_format_and_defaults_to_tools() -> None:
    seen: list[dict[str, object]] = []
    provider = OllamaProvider(
        model="qwen2.5-coder",
        transport=httpx.MockTransport(_capture(seen)),
    )
    assert provider.supports_tools() is True
    result = await provider.structured_complete([Message(role="user", content="x")], Answer)
    await provider.close()
    assert result.value == 7
    assert "response_format" not in seen[0]
    assert seen[0]["format"]["properties"]["value"]["type"] == "integer"


@pytest.mark.asyncio
async def test_vllm_structured_uses_guided_json() -> None:
    seen: list[dict[str, object]] = []
    provider = VLLMProvider(model="local-coder", transport=httpx.MockTransport(_capture(seen)))
    result = await provider.structured_complete([Message(role="user", content="x")], Answer)
    await provider.close()
    assert result.value == 7
    assert "response_format" not in seen[0]
    assert seen[0]["guided_json"]["properties"]["value"]["type"] == "integer"


@pytest.mark.asyncio
async def test_structured_falls_back_when_constraint_rejected() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "guided_json" in payload:
            return httpx.Response(400, json={"error": "guided_json not supported"})
        return response(json.dumps({"value": 9}))

    provider = VLLMProvider(model="local-coder", transport=httpx.MockTransport(handler))
    result = await provider.structured_complete([Message(role="user", content="x")], Answer)
    await provider.close()
    assert result.value == 9
    assert "guided_json" in calls[0]
    assert "guided_json" not in calls[1]


@pytest.mark.asyncio
async def test_complete_sends_tools_and_parses_tool_calls() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "mock",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "a.py"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = OllamaProvider(model="qwen2.5-coder", transport=httpx.MockTransport(handler))
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    result = await provider.complete(
        [Message(role="user", content="read a.py")], tools=tools, tool_choice="required"
    )
    await provider.close()
    assert seen[0]["tools"] == tools
    assert seen[0]["tool_choice"] == "required"
    assert result.tool_calls == [
        ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})
    ]


@pytest.mark.asyncio
async def test_tool_shape_rejection_is_distinct_from_provider_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "tool parser unavailable"})

    provider = OllamaProvider(model="mock", transport=httpx.MockTransport(handler))
    with pytest.raises(ToolCallingUnsupported):
        await provider.complete(
            [Message(role="user", content="x")],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        )
    await provider.close()


@pytest.mark.asyncio
async def test_auth_failure_with_tools_is_not_misclassified() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    provider = OllamaProvider(model="mock", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            [Message(role="user", content="x")],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        )
    assert not isinstance(caught.value, ToolCallingUnsupported)
    await provider.close()


@pytest.mark.asyncio
async def test_tool_message_thread_serializes_to_wire_format() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response("done")

    provider = OllamaProvider(model="qwen2.5-coder", transport=httpx.MockTransport(handler))
    messages = [
        Message(role="user", content="read a.py"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
        ),
        Message(role="tool", content="contents", tool_call_id="call_1"),
    ]
    await provider.complete(messages)
    await provider.close()
    wire = seen[0]["messages"]
    assert wire[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire[1]["content"] is None
    assert wire[2]["role"] == "tool"
    assert wire[2]["tool_call_id"] == "call_1"


def test_factory_builds_ollama_provider_with_default_features() -> None:
    provider = create_provider(
        "local-ollama",
        ProviderConfig(
            type="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen2.5-coder",
            reasoning_effort="high",
        ),
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.supports_tools() is True
    assert provider.supports_json_schema() is True
    assert provider.reasoning_effort == "high"


def _truncated_json_reply(request: httpx.Request) -> httpx.Response:
    """A reply cut off mid-string, exactly as an output-token limit produces."""
    return httpx.Response(
        200,
        json={
            "model": "mock",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{\n "thought": "t",\n "action": "write",\n'
                        ' "path": "landing.html",\n "content": "<!DOCTYPE html><html>',
                    },
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 4096},
        },
    )


@pytest.mark.asyncio
async def test_truncated_structured_reply_names_the_token_limit() -> None:
    """The old message was 'Unterminated string at line 5', which explains nothing."""
    provider = OllamaProvider(
        model="qwen2.5-coder",
        max_output_tokens=4096,
        transport=httpx.MockTransport(_truncated_json_reply),
    )

    with pytest.raises(ProviderError) as caught:
        await provider.structured_complete([Message(role="user", content="restyle it")], Answer)
    await provider.close()

    message = str(caught.value)
    assert "4096-token output limit" in message
    assert "smaller, targeted change" in message
    assert "max_output_tokens" in message
    assert "Unterminated string" not in message


@pytest.mark.asyncio
async def test_truncation_retry_asks_for_a_smaller_edit_not_a_resend() -> None:
    """Echoing the truncated reply back would spend the budget that was already short."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _truncated_json_reply(request)

    provider = OllamaProvider(
        model="qwen2.5-coder",
        max_output_tokens=4096,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError):
        await provider.structured_complete([Message(role="user", content="restyle it")], Answer)
    await provider.close()

    retry = seen[1]["messages"][-1]
    assert retry["role"] == "user"
    assert "cut off at the output token limit" in retry["content"]
    assert "replace action" in retry["content"]
    # The truncated body must not be fed back to the model.
    assert "<!DOCTYPE html>" not in json.dumps(seen[1]["messages"])


@pytest.mark.asyncio
async def test_a_malformed_but_complete_reply_still_reports_the_schema_error() -> None:
    """Truncation handling must not swallow ordinary validation failures."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "mock",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"value": "not-a-number"}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = OllamaProvider(model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="Invalid structured response"):
        await provider.structured_complete([Message(role="user", content="x")], Answer)
    await provider.close()


def test_output_token_default_is_large_enough_for_a_real_file() -> None:
    assert DEFAULT_MAX_OUTPUT_TOKENS >= 16_384
    assert ProviderConfig(type="ollama", base_url="http://x/v1", model="m").max_output_tokens == (
        DEFAULT_MAX_OUTPUT_TOKENS
    )


# --------------------------------------------------------------------------
# Listing the models a provider actually has
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_lists_installed_models_from_the_native_endpoint() -> None:
    """The picker needs sizes and capabilities, which only /api/tags carries."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3.8:27b-mlx",
                        "model": "qwen3.8:27b-mlx",
                        "size": 18174721847,
                        "details": {"parameter_size": "27B", "quantization_level": "nvfp4"},
                        "capabilities": ["completion", "tools"],
                    }
                ]
            },
        )

    provider = OllamaProvider(model="", transport=httpx.MockTransport(handler))
    try:
        items = await provider.list_models()
    finally:
        await provider.close()

    assert seen == ["http://127.0.0.1:11434/api/tags"]
    assert items[0]["model"] == "qwen3.8:27b-mlx"


@pytest.mark.asyncio
async def test_ollama_falls_back_to_the_openai_model_endpoint() -> None:
    """Not every Ollama-compatible server serves the native tags endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "llama3.2"}]})

    provider = OllamaProvider(model="", transport=httpx.MockTransport(handler))
    try:
        items = await provider.list_models()
    finally:
        await provider.close()

    assert items == [{"id": "llama3.2"}]


@pytest.mark.asyncio
async def test_an_ollama_with_no_models_says_how_to_pull_one() -> None:
    provider = OllamaProvider(
        model="",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []})),
    )
    with pytest.raises(ProviderError, match="ollama pull"):
        try:
            await provider.list_models()
        finally:
            await provider.close()


def test_installed_ollama_models_are_shaped_for_the_picker() -> None:
    """A bare tag is unreadable; size and capabilities make it a choice."""
    from daino.application.provider_service import ProviderApplicationService

    models = ProviderApplicationService._ollama_models(
        [
            {
                "model": "qwen3.8:27b-mlx",
                "size": 18174721847,
                "details": {"parameter_size": "27B", "quantization_level": "nvfp4"},
                "capabilities": ["tools", "thinking"],
            },
            {"model": ""},
        ]
    )

    assert len(models) == 1, "an entry with no identifier is not selectable"
    assert models[0].id == "qwen3.8:27b-mlx"
    assert "16.9 GB" in models[0].label
    assert "27B" in models[0].label
    assert "tools, thinking" in models[0].label
    assert models[0].label.count("qwen3.8:27b-mlx") == 1, "the tag must not be printed twice"
