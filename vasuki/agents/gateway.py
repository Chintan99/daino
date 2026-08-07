"""Audited model gateway shared by all specialist agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TypeVar

from pydantic import BaseModel

from vasuki.config.models import ProviderConfig, Settings
from vasuki.events import AgentRoleChanged, EventBus, ModelSelected
from vasuki.model_router import ModelRole, ModelRouter, RoutingContext
from vasuki.model_router.router import ModelSelection
from vasuki.persistence import Database
from vasuki.persistence.models import ModelCall
from vasuki.providers import create_provider
from vasuki.schemas import LLMResponse, Message
from vasuki.utils.ids import new_id

StructuredT = TypeVar("StructuredT", bound=BaseModel)


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        events: EventBus | None = None,
        *,
        profile_override: str = "",
    ) -> None:
        self.settings = settings
        self.database = database
        self.router = ModelRouter(settings)
        self.events = events
        self.profile_override = profile_override

    def with_profile(self, profile_override: str) -> ModelGateway:
        """Return a gateway pinned to one model profile for every role.

        Agents receive a gateway rather than a routing decision, so binding here
        applies a session model choice across the whole agent graph without every
        specialist having to thread the selection through its own signature.
        """
        if not profile_override or profile_override == self.profile_override:
            return self
        return ModelGateway(
            self.settings,
            self.database,
            self.events,
            profile_override=profile_override,
        )

    def _emit_selection(
        self,
        mission_id: str,
        role: ModelRole,
        profile_name: str,
        provider: str,
        model: str,
    ) -> None:
        if self.events is None:
            return
        self.events.publish(AgentRoleChanged(mission_id=mission_id, role=role.value))
        self.events.publish(
            ModelSelected(
                mission_id=mission_id,
                profile=profile_name,
                provider=provider,
                model=model,
                role=role.value,
            )
        )

    async def structured(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        routing_context: RoutingContext | None = None,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> StructuredT:
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        self._emit_selection(
            mission_id,
            role,
            selection.profile_name,
            selection.profile.provider,
            selection.profile.model,
        )
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(
                f"Model {selection.profile_name} references missing provider "
                f"{selection.profile.provider}"
            )
        config = _resolved_config(provider_config, selection)
        provider = create_provider(selection.profile.provider, config)
        record = ModelCall(
            id=new_id("model-call"),
            mission_id=mission_id,
            role=role.value,
            provider=selection.profile.provider,
            model=selection.profile.model,
            selection_reason=selection.reason,
            included_files=included_files or [],
            success=False,
        )
        try:
            result = await provider.structured_complete(messages, schema)
            record.success = True
            return result
        finally:
            await provider.close()
            with self.database.session() as session:
                session.add(record)

    def route_supports_tools(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        profile_override: str | None = None,
    ) -> bool:
        """Report whether the provider this role routes to advertises native tool calling."""
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        provider_config = self.settings.providers.get(selection.profile.provider)
        return provider_config is not None and "tools" in provider_config.features

    async def complete(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        routing_context: RoutingContext | None = None,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> LLMResponse:
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        self._emit_selection(
            mission_id,
            role,
            selection.profile_name,
            selection.profile.provider,
            selection.profile.model,
        )
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(f"Missing provider {selection.profile.provider}")
        config = _resolved_config(provider_config, selection)
        provider = create_provider(selection.profile.provider, config)
        effective_tools = tools if provider.supports_tools() else None
        response: LLMResponse | None = None
        try:
            response = await provider.complete(
                messages, tools=effective_tools, tool_choice=tool_choice
            )
            return response
        finally:
            await provider.close()
            with self.database.session() as session:
                session.add(
                    ModelCall(
                        id=new_id("model-call"),
                        mission_id=mission_id,
                        role=role.value,
                        provider=selection.profile.provider,
                        model=selection.profile.model,
                        selection_reason=selection.reason,
                        included_files=included_files or [],
                        input_tokens=response.input_tokens if response else 0,
                        output_tokens=response.output_tokens if response else 0,
                        latency_ms=response.latency_ms if response else 0,
                        success=response is not None,
                    )
                )

    async def stream(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        *,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream from the selected provider and persist the audited model call."""
        selection = self.router.select(
            role,
            profile_override=profile_override or self.profile_override,
        )
        self._emit_selection(
            mission_id,
            role,
            selection.profile_name,
            selection.profile.provider,
            selection.profile.model,
        )
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(f"Missing provider {selection.profile.provider}")
        config = _resolved_config(provider_config, selection)
        provider = create_provider(selection.profile.provider, config)
        success = False
        try:
            async for chunk in provider.stream(messages):
                yield chunk
            success = True
        finally:
            await provider.close()
            with self.database.session() as session:
                session.add(
                    ModelCall(
                        id=new_id("model-call"),
                        mission_id=mission_id,
                        role=role.value,
                        provider=selection.profile.provider,
                        model=selection.profile.model,
                        selection_reason=selection.reason,
                        included_files=included_files or [],
                        success=success,
                    )
                )


def _resolved_config(provider_config: ProviderConfig, selection: ModelSelection) -> ProviderConfig:
    """Apply the chosen profile's model and output ceiling to the provider config.

    The profile is the per-model half of the routing decision, so a profile that
    raises max_output_tokens for a model with a bigger window has to actually
    reach the provider. Previously only the model name was carried over and the
    profile's ceiling was silently ignored.
    """
    return provider_config.model_copy(
        update={
            "model": selection.profile.model,
            "max_output_tokens": selection.profile.max_output_tokens,
        }
    )
