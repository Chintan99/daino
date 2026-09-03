"""One local model, one request at a time.

A local runtime holds a single copy of a single model. Asking it for several
completions at once does not make it faster: the requests queue inside the
server, every one of them returns later than it would have alone, and the
client's timeout is running the whole time it waits. These tests pin the gate
that keeps daino's own fan-outs (a QA scan, a team run) from doing that.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from daino.config.models import ProviderConfig
from daino.providers import create_provider
from daino.providers.gate import active_limits, request_slot, reset
from daino.schemas import Message


@pytest.fixture(autouse=True)
def clean_gates() -> None:
    reset()


class Recorder:
    """A transport that records how many requests overlap."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def transport(self) -> httpx.AsyncBaseTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.in_flight -= 1
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        return httpx.MockTransport(handler)


def _provider(
    recorder: Recorder,
    provider_type: str,
    name: str,
    *,
    base_url: str = "http://127.0.0.1:9/v1",
    **overrides: object,
):
    config = ProviderConfig(
        type=provider_type,  # type: ignore[arg-type]
        base_url=base_url,
        model="m",
        **overrides,  # type: ignore[arg-type]
    )
    provider = create_provider(name, config)
    provider.client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=base_url.rstrip("/") + "/",
        transport=recorder.transport(),
    )
    return provider


async def _fan_out(provider, count: int = 5) -> None:
    message = [Message(role="user", content="hi")]
    await asyncio.gather(*(provider.complete(message, max_tokens=1) for _ in range(count)))


async def test_a_local_provider_serialises_its_requests() -> None:
    recorder = Recorder()
    provider = _provider(recorder, "ollama", "local-ollama")
    try:
        await _fan_out(provider, 5)
    finally:
        await provider.close()
    assert recorder.calls == 5
    assert recorder.peak == 1, "concurrent requests reached the model server"


async def test_a_hosted_provider_is_not_serialised() -> None:
    """Serialising OpenRouter would make a QA scan five times slower for nothing."""
    recorder = Recorder()
    provider = _provider(recorder, "openrouter", "openrouter", api_key="")
    try:
        await _fan_out(provider, 5)
    finally:
        await provider.close()
    assert recorder.peak == 5


async def test_the_limit_is_configurable() -> None:
    recorder = Recorder()
    provider = _provider(recorder, "ollama", "tuned-ollama", max_concurrent_requests=2)
    try:
        await _fan_out(provider, 6)
    finally:
        await provider.close()
    assert recorder.peak == 2

    # And an explicit 0 opts out entirely.
    reset()
    recorder = Recorder()
    provider = _provider(recorder, "ollama", "unlimited", max_concurrent_requests=0)
    try:
        await _fan_out(provider, 4)
    finally:
        await provider.close()
    assert recorder.peak == 4


async def test_two_model_servers_do_not_block_each_other() -> None:
    """The queue belongs to the server, so a second host runs independently."""
    first, second = Recorder(delay=0.08), Recorder(delay=0.08)
    local = _provider(first, "ollama", "local-a", base_url="http://127.0.0.1:9/v1")
    remote = _provider(second, "ollama", "local-b", base_url="http://10.0.0.9:11434/v1")
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.gather(_fan_out(local, 3), _fan_out(remote, 3))
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await local.close()
        await remote.close()
    assert first.peak == 1 and second.peak == 1, "each server was served serially"
    # Serial per server, parallel across them: six 80ms requests in ~3 rounds.
    assert elapsed < 0.08 * 6 * 0.9, f"the two servers were serialised together ({elapsed:.2f}s)"
    assert set(active_limits()) == {
        "http://127.0.0.1:9/v1",
        "http://10.0.0.9:11434/v1",
    }


async def test_two_entries_for_one_server_share_its_queue() -> None:
    """Two profiles on the same Ollama are still one model server."""
    recorder = Recorder()
    first = _provider(recorder, "ollama", "coder")
    second = _provider(recorder, "ollama", "summariser")
    try:
        await asyncio.gather(_fan_out(first, 3), _fan_out(second, 3))
    finally:
        await first.close()
        await second.close()
    assert recorder.peak == 1
    assert list(active_limits()) == ["http://127.0.0.1:9/v1"]


async def test_the_gate_is_a_no_op_when_unlimited() -> None:
    async with request_slot("anything", 0):
        async with request_slot("anything", 0):
            pass  # would deadlock if a semaphore of 0 were created
