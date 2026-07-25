"""Audited model gateway shared by all specialist agents."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from vasuki.config.models import Settings
from vasuki.model_router import ModelRole, ModelRouter, RoutingContext
from vasuki.persistence import Database
from vasuki.persistence.models import ModelCall
from vasuki.providers import create_provider
from vasuki.schemas import LLMResponse, Message
from vasuki.utils.ids import new_id

StructuredT = TypeVar("StructuredT", bound=BaseModel)


class ModelGateway:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.router = ModelRouter(settings)

    async def structured(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        routing_context: RoutingContext | None = None,
        included_files: list[str] | None = None,
    ) -> StructuredT:
        selection = self.router.select(role, routing_context)
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(
                f"Model {selection.profile_name} references missing provider "
                f"{selection.profile.provider}"
            )
        config = provider_config.model_copy(update={"model": selection.profile.model})
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

    async def complete(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        *,
        included_files: list[str] | None = None,
    ) -> LLMResponse:
        selection = self.router.select(role)
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(f"Missing provider {selection.profile.provider}")
        config = provider_config.model_copy(update={"model": selection.profile.model})
        provider = create_provider(selection.profile.provider, config)
        response: LLMResponse | None = None
        try:
            response = await provider.complete(messages)
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
