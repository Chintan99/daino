from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daino.application import (
    MissionApplicationService,
    ProviderApplicationService,
    open_project,
)
from daino.events import EventBus, MissionCreated, ModelReasoningChunk, ModelStreamChunk
from daino.exceptions import ProviderError
from daino.persistence.models import ConversationMessage, ConversationSession, MissionEventRecord
from daino.security import resolve_secret


def test_event_bus_delivers_typed_events() -> None:
    bus = EventBus()
    received: list[object] = []
    subscription = bus.subscribe(received.append)
    event = MissionCreated(mission_id="M-1", request="Add health", mode="direct")

    bus.publish(event)

    assert received == [event]
    assert event.payload()["request"] == "Add health"
    subscription.close()
    bus.publish(ModelStreamChunk(mission_id="M-1", content="ignored"))
    assert received == [event]


def test_project_event_sink_and_conversation_survive_reopen(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    first = open_project(root)
    service = MissionApplicationService(first)
    session_id = service.create_session("Health mission")
    service.add_message(
        session_id,
        kind="user",
        role="user",
        content="Add a health endpoint",
    )
    first.events.publish(
        MissionCreated(
            mission_id=None,
            request="General repository question",
            mode="direct",
        )
    )
    with first.database.session() as session:
        assert session.query(MissionEventRecord).count() == 1
    first.close()

    second = open_project(root)
    restored = MissionApplicationService(second).messages(session_id)
    assert [item.content for item in restored] == ["Add a health endpoint"]
    assert restored[0].kind == "user"
    second.close()


def test_live_model_chunks_are_not_persisted(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)

    context.events.publish(
        ModelReasoningChunk(
            mission_id="M-live",
            content="provider reasoning",
            role="builder",
            provider="local-ollama",
            model="qwen",
            profile="local",
        )
    )
    context.events.publish(ModelStreamChunk(mission_id="M-live", content="answer"))

    with context.database.session() as session:
        assert session.query(MissionEventRecord).count() == 0
    context.close()


def test_verbose_display_mode_is_scoped_to_the_conversation(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = MissionApplicationService(context)
    first = service.create_session("first")
    second = service.create_session("second")

    assert service.verbose_enabled(first)
    service.set_verbose(first, True)

    assert service.verbose_enabled(first)
    assert service.verbose_enabled(second)
    service.set_verbose(first, False)
    assert not service.verbose_enabled(first)
    context.close()


def test_mission_links_survive_later_turns_without_appearing_in_history(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = MissionApplicationService(context)
    session_id = service.create_session("live usage")
    first = service.core.create("first failed turn")
    service.attach_session_mission(session_id, first.id)
    second = service.core.create("second turn")

    service.attach_session_mission(session_id, second.id)

    with context.database.session() as session:
        stored = session.get(ConversationSession, session_id)
        assert stored is not None
        assert stored.mission_id == second.id
        assert (
            session.query(ConversationMessage)
            .filter_by(session_id=session_id, mission_id=first.id)
            .one()
            .kind
            == "mission_link"
        )
    assert service.messages(session_id) == []
    context.close()


def test_provider_service_configures_local_openai_compatible_model(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)

    service.add(
        name="local-ollama",
        provider_type="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5-coder",
    )

    assert context.settings.providers["local-ollama"].api_key == ""
    assert context.settings.providers["local-ollama"].features == ["chat", "structured", "tools"]
    assert context.settings.models["local-ollama"].local is True
    assert context.settings.routing["builder"] == "local-ollama"
    context.close()


def test_session_effort_updates_selected_remote_profile_only(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="remote",
        provider_type="openai-compatible",
        base_url="https://models.example.invalid/v1",
        model="reasoning-model",
    )
    session_id = MissionApplicationService(context).create_session()

    profile, effort = service.set_session_effort(session_id, "high")

    assert profile == "remote"
    assert effort == "high"
    assert service.session_effort(session_id) == "high"
    assert context.settings.models["remote"].reasoning_effort == "high"
    service.set_session_effort(session_id, "auto")
    assert context.settings.models["remote"].reasoning_effort is None
    context.close()


def test_connecting_another_provider_reroutes_every_agent_role(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="local-ollama",
        provider_type="openai-compatible",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5-coder",
    )

    rerouted = service.add(
        name="openrouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="vendor/model",
        api_key_reference="env://OPENROUTER_API_KEY",
    )

    assert set(rerouted) == set(context.settings.routing)
    assert set(context.settings.routing.values()) == {"openrouter"}
    assert service.routable_profile() == "openrouter"
    context.close()


def test_secondary_provider_can_be_added_without_stealing_the_routing(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="primary",
        provider_type="openai-compatible",
        base_url="http://127.0.0.1:8000/v1",
        model="primary-model",
    )

    rerouted = service.add(
        name="spare",
        provider_type="openai-compatible",
        base_url="http://127.0.0.1:9000/v1",
        model="spare-model",
        make_default=False,
    )

    assert rerouted == []
    assert set(context.settings.routing.values()) == {"primary"}
    context.close()


def test_routes_pointing_at_a_removed_provider_are_repaired(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    context.settings.routing = {"builder": "deleted-profile", "architect": "deleted-profile"}

    service.add(
        name="replacement",
        provider_type="openai-compatible",
        base_url="http://127.0.0.1:8000/v1",
        model="replacement-model",
        make_default=False,
    )

    assert not service.route_is_usable("deleted-profile")
    assert context.settings.routing["builder"] == "replacement"
    assert context.settings.routing["architect"] == "replacement"
    context.close()


def test_project_provider_override_can_return_to_global_settings(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="shared",
        provider_type="openai-compatible",
        base_url="https://global.example.invalid/v1",
        model="global-model",
        scope="global",
    )
    service.add(
        name="project-only",
        provider_type="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="project-model",
        scope="project",
    )

    assert service.routable_profile() == "project-only"
    assert service.use_global() == "shared"
    assert service.routable_profile() == "shared"
    assert "project-only" not in context.settings.providers
    project_config = (root / ".daino" / "config.yaml").read_text(encoding="utf-8")
    assert "project-only" not in project_config
    context.close()


@pytest.mark.asyncio
async def test_wait_for_event() -> None:
    bus = EventBus()

    async def publish() -> None:
        bus.publish(ModelStreamChunk(mission_id="M-2", content="hello"))

    task = asyncio.create_task(bus.wait_for(ModelStreamChunk, timeout=1))
    await asyncio.sleep(0)
    await publish()
    event = await task
    assert isinstance(event, ModelStreamChunk)
    assert event.content == "hello"


@pytest.mark.asyncio
async def test_openrouter_configuration_validates_then_stores_secret_reference(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    seen_keys: list[str] = []

    class FakeOpenRouter:
        def __init__(self, *, api_key: str, **_: object) -> None:
            seen_keys.append(api_key)

        async def validate_key(self) -> dict[str, object]:
            return {"label": "test-key", "limit_remaining": 5}

        async def list_models(self) -> list[dict[str, object]]:
            return [
                {
                    "id": "openai/test-model",
                    "name": "Test Model",
                    "context_length": 32_000,
                }
            ]

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "daino.application.provider_service.OpenRouterProvider",
        FakeOpenRouter,
    )
    service = ProviderApplicationService(context)

    status, models = await service.configure(
        name="openrouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="openai/test-model",
        api_key_input="sk-or-valid-test-key",
    )

    reference = context.settings.providers["openrouter"].api_key
    assert status.connected is True
    assert [item.id for item in models] == ["openai/test-model"]
    assert seen_keys == ["sk-or-valid-test-key"]
    assert reference.startswith("file://")
    assert resolve_secret(reference) == "sk-or-valid-test-key"
    assert "sk-or-valid-test-key" not in (root / ".daino" / "config.yaml").read_text(
        encoding="utf-8"
    )
    context.close()


@pytest.mark.asyncio
async def test_invalid_openrouter_key_is_not_saved(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)

    class InvalidOpenRouter:
        def __init__(self, **_: object) -> None:
            pass

        async def validate_key(self) -> dict[str, object]:
            raise ProviderError("OpenRouter API key rejected (HTTP 401): User not found.")

        async def list_models(self) -> list[dict[str, object]]:
            raise AssertionError("models must not be fetched after invalid authentication")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "daino.application.provider_service.OpenRouterProvider",
        InvalidOpenRouter,
    )
    service = ProviderApplicationService(context)

    with pytest.raises(ValueError, match=r"not saved.*HTTP 401.*User not found"):
        await service.configure(
            name="openrouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="openai/test-model",
            api_key_input="invalid-key",
        )

    assert "openrouter" not in context.settings.providers
    assert not (root / ".daino" / "secrets").exists()
    context.close()
