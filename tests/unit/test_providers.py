from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from vasuki.config.models import ProviderConfig
from vasuki.exceptions import ProviderError
from vasuki.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
from vasuki.providers.factory import create_provider
from vasuki.providers.ollama import OllamaProvider
from vasuki.providers.openai_compatible import OpenAICompatibleProvider
from vasuki.providers.openrouter import OpenRouterProvider
from vasuki.providers.vllm import VLLMProvider
from vasuki.schemas import Message, ToolCall


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
        application_name="Vasuki",
        referring_url="https://example.invalid",
        transport=transport,
    )
    await openrouter.complete([Message(role="user", content="test")])
    await openrouter.close()
    vllm = VLLMProvider(model="mock", api_key="", transport=transport)
    await vllm.complete([Message(role="user", content="test")])
    await vllm.close()
    assert seen[0].headers["x-title"] == "Vasuki"
    assert seen[0].headers["http-referer"] == "https://example.invalid"
    assert "authorization" not in seen[1].headers


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
        ),
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.supports_tools() is True
    assert provider.supports_json_schema() is True


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
