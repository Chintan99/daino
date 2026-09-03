from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import BaseModel

from daino.agents.gateway import ModelGateway
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings
from daino.events import EventBus, MissionEvent, ModelReasoningChunk
from daino.model_router import ModelRole
from daino.schemas import LLMResponse, Message


class Answer(BaseModel):
    value: int


class RecordingDatabase:
    def __init__(self) -> None:
        self.records: list[Any] = []

    @contextmanager
    def session(self) -> Iterator[RecordingDatabase]:
        yield self

    def add(self, record: Any) -> None:
        self.records.append(record)


class ReasoningProvider:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.reasoning_handler: Callable[[str], None] | None = None
        #: Every handler this adapter was given, in order. The gateway now
        #: borrows adapters from a pool and clears the handler on return, so
        #: "was a handler attached for the call?" can no longer be answered by
        #: looking at the adapter afterwards — a detached handler is the
        #: correct end state, not a missing one.
        self.handler_history: list[Callable[[str], None] | None] = []

    def set_reasoning_handler(self, handler: Callable[[str], None] | None) -> None:
        self.reasoning_handler = handler
        self.handler_history.append(handler)

    def assert_handler_attached_then_cleared(self) -> None:
        assert any(handler is not None for handler in self.handler_history)
        assert self.reasoning_handler is None

    def supports_tools(self) -> bool:
        return True

    def _reason(self, content: str) -> None:
        assert self.reasoning_handler is not None
        self.reasoning_handler(content)

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self._reason("Inspecting the request")
        self.timeline.append("provider:complete")
        return LLMResponse(content="done", model="qwen", provider="local-ollama")

    async def structured_complete(
        self,
        messages: list[Message],
        schema: type[Answer],
        **kwargs: object,
    ) -> Answer:
        self._reason("Preparing structured output")
        self.timeline.append("provider:structured")
        return schema(value=42)

    async def stream(self, messages: list[Message]):  # type: ignore[no-untyped-def]
        self._reason("Preparing the answer")
        self.timeline.append("provider:stream")
        yield "answer"

    async def close(self) -> None:
        return None


def configured_settings() -> Settings:
    settings = Settings()
    settings.providers = {
        "local-ollama": ProviderConfig(
            type="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="provider-default",
        )
    }
    settings.models = {
        "local-profile": ModelProfileConfig(
            provider="local-ollama",
            model="qwen",
            local=True,
        )
    }
    settings.routing = {"builder": "local-profile"}
    return settings


def gateway_with_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModelGateway, ReasoningProvider, list[MissionEvent], list[str]]:
    timeline: list[str] = []
    provider = ReasoningProvider(timeline)
    events: list[MissionEvent] = []
    bus = EventBus()

    def record(event: MissionEvent) -> None:
        events.append(event)
        timeline.append(f"event:{event.kind}")

    bus.subscribe(record)
    monkeypatch.setattr(
        "daino.agents.gateway.create_provider",
        lambda _name, _config: provider,
    )
    gateway = ModelGateway(  # type: ignore[arg-type]
        configured_settings(),
        RecordingDatabase(),
        bus,
    )
    return gateway, provider, events, timeline


def assert_reasoning_event(events: list[MissionEvent], content: str) -> None:
    assert [event.kind for event in events] == [
        "AgentRoleChanged",
        "ModelSelected",
        "ModelReasoningChunk",
    ]
    event = events[-1]
    assert isinstance(event, ModelReasoningChunk)
    assert event.mission_id == "mission-live"
    assert event.content == content
    assert event.role == "builder"
    assert event.provider == "local-ollama"
    assert event.model == "qwen"
    assert event.profile == "local-profile"
    payload = event.payload()
    assert payload["details"] == {}
    assert set(payload) == {
        "mission_id",
        "timestamp",
        "details",
        "content",
        "role",
        "provider",
        "model",
        "profile",
    }


@pytest.mark.asyncio
async def test_complete_publishes_tagged_reasoning_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, provider, events, timeline = gateway_with_provider(monkeypatch)

    response = await gateway.complete(
        "mission-live",
        ModelRole.BUILDER,
        [Message(role="user", content="work")],
    )

    assert response.content == "done"
    provider.assert_handler_attached_then_cleared()
    assert_reasoning_event(events, "Inspecting the request")
    assert timeline.index("event:ModelReasoningChunk") < timeline.index("provider:complete")


@pytest.mark.asyncio
async def test_structured_publishes_tagged_reasoning_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, provider, events, timeline = gateway_with_provider(monkeypatch)

    response = await gateway.structured(
        "mission-live",
        ModelRole.BUILDER,
        [Message(role="user", content="work")],
        Answer,
    )

    assert response.value == 42
    provider.assert_handler_attached_then_cleared()
    assert_reasoning_event(events, "Preparing structured output")
    assert timeline.index("event:ModelReasoningChunk") < timeline.index("provider:structured")


@pytest.mark.asyncio
async def test_stream_publishes_tagged_reasoning_before_answer_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, provider, events, timeline = gateway_with_provider(monkeypatch)

    chunks = []
    async for chunk in gateway.stream(
        "mission-live",
        ModelRole.BUILDER,
        [Message(role="user", content="work")],
    ):
        timeline.append(f"consumer:{chunk}")
        chunks.append(chunk)

    assert chunks == ["answer"]
    provider.assert_handler_attached_then_cleared()
    assert_reasoning_event(events, "Preparing the answer")
    assert timeline.index("event:ModelReasoningChunk") < timeline.index("consumer:answer")
