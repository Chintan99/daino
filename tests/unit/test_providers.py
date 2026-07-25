from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from vasuki.providers.openai_compatible import OpenAICompatibleProvider
from vasuki.providers.openrouter import OpenRouterProvider
from vasuki.providers.vllm import VLLMProvider
from vasuki.schemas import Message


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
