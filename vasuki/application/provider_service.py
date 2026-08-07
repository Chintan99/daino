"""Provider and session-scoped model selection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from vasuki.application.context import ProjectContext
from vasuki.application.view_models import OpenRouterModel, ProviderStatus
from vasuki.config import save_settings
from vasuki.config.globals import save_global
from vasuki.config.models import ModelProfileConfig, ProviderConfig
from vasuki.events import ModelSelected
from vasuki.model_router import ModelRole
from vasuki.persistence.models import ConversationSession, Provider
from vasuki.providers import OpenRouterProvider, create_provider
from vasuki.security import redact, resolve_secret, store_project_secret
from vasuki.utils.ids import new_id


class ProviderApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    def providers(self) -> list[ProviderStatus]:
        return [
            ProviderStatus(
                name=name,
                type=config.type,
                base_url=config.base_url,
                model=config.model,
            )
            for name, config in self.context.settings.providers.items()
        ]

    def route_is_usable(self, profile_name: str | None) -> bool:
        """Report whether a routed profile still resolves to a configured provider."""
        if not profile_name:
            return False
        profile = self.context.settings.models.get(profile_name)
        return profile is not None and profile.provider in self.context.settings.providers

    def routable_profile(self) -> str:
        """Return a usable model profile for chat, preferring the configured routing."""
        routing = self.context.settings.routing
        for role in ModelRole:
            candidate = routing.get(role.value)
            if self.route_is_usable(candidate):
                return str(candidate)
        for name in self.context.settings.models:
            if self.route_is_usable(name):
                return name
        return ""

    def _apply_routing(self, profile_name: str, *, make_default: bool) -> list[str]:
        """Point agent roles at ``profile_name``.

        Routes that no longer resolve to a configured provider are always repaired;
        otherwise the profile only takes over every role when it is made the default.
        Without this a provider connected after the first one is validated, saved,
        and then never used, because every role still points at the original.
        """
        routing = self.context.settings.routing
        rerouted: list[str] = []
        for role in ModelRole:
            current = routing.get(role.value)
            if not make_default and self.route_is_usable(current):
                continue
            if current != profile_name:
                rerouted.append(role.value)
            routing[role.value] = profile_name
        return rerouted

    def add(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        model: str,
        api_key_reference: str = "",
        make_default: bool = True,
    ) -> list[str]:
        """Add or update a provider without accepting literal secret values.

        Returns the agent roles that were re-routed to the provider.
        """
        normalized = name.strip()
        if not normalized:
            raise ValueError("Provider name is required")
        provider = ProviderConfig(
            type=provider_type,
            base_url=base_url.strip(),
            model=model.strip(),
            api_key=api_key_reference.strip(),
            timeout=300 if provider_type in {"ollama", "vllm"} else 120,
        )
        host = urlparse(provider.base_url).hostname or ""
        local = provider_type in {"ollama", "vllm"} or host in {"localhost", "127.0.0.1", "::1"}
        self.context.settings.providers[normalized] = provider
        self.context.settings.models[normalized] = ModelProfileConfig(
            provider=normalized,
            model=provider.model,
            local=local,
        )
        rerouted = self._apply_routing(normalized, make_default=make_default)
        # Providers and their routing are user-level: a model connected in one
        # checkout should be available in the next one without repeating this.
        save_global(self.context.settings)
        save_settings(self.context.settings, self.context.root)
        with self.context.database.session() as session:
            stored = session.scalar(select(Provider).where(Provider.name == normalized))
            if stored:
                stored.type = provider.type
                stored.base_url = provider.base_url
                stored.api_key_reference = provider.api_key
                stored.config = provider.model_dump(mode="json")
            else:
                session.add(
                    Provider(
                        id=new_id("provider"),
                        name=normalized,
                        type=provider.type,
                        base_url=provider.base_url,
                        api_key_reference=provider.api_key,
                        config=provider.model_dump(mode="json"),
                    )
                )
        return rerouted

    @staticmethod
    def _openrouter_models(items: list[dict[str, Any]]) -> list[OpenRouterModel]:
        models: list[OpenRouterModel] = []
        for item in items:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            pricing = item.get("pricing")
            pricing = pricing if isinstance(pricing, dict) else {}
            try:
                context_length = int(item.get("context_length") or 0)
            except (TypeError, ValueError):
                context_length = 0
            models.append(
                OpenRouterModel(
                    id=model_id,
                    name=str(item.get("name") or model_id),
                    context_length=context_length,
                    prompt_price=str(pricing.get("prompt") or ""),
                    completion_price=str(pricing.get("completion") or ""),
                )
            )
        return sorted(models, key=lambda item: (item.name.casefold(), item.id))

    @staticmethod
    def _secret_value(value_or_reference: str) -> str:
        value = value_or_reference.strip()
        if value.startswith(("env://", "file://", "keyring://")):
            return resolve_secret(value)
        return value

    async def openrouter_models(
        self,
        *,
        api_key_input: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> list[OpenRouterModel]:
        api_key = self._secret_value(api_key_input)
        provider = OpenRouterProvider(
            api_key=api_key,
            model="openrouter/auto",
            base_url=base_url.strip() or "https://openrouter.ai/api/v1",
            max_retries=0,
        )
        try:
            return self._openrouter_models(await provider.list_models())
        finally:
            await provider.close()

    async def configure(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        model: str,
        api_key_input: str = "",
        make_default: bool = True,
    ) -> tuple[ProviderStatus, list[OpenRouterModel]]:
        """Validate a provider before saving it, securely storing literal OpenRouter keys."""
        if provider_type != "openrouter":
            rerouted = self.add(
                name=name,
                provider_type=provider_type,
                base_url=base_url,
                model=model,
                api_key_reference=api_key_input,
                make_default=make_default,
            )
            status = await self.health(name)
            return (
                replace(status, detail=self._routing_detail(status.detail, rerouted)),
                [],
            )

        normalized = name.strip()
        existing = self.context.settings.providers.get(normalized)
        supplied = api_key_input.strip() or (
            existing.api_key if existing and existing.type == "openrouter" else ""
        )
        if not supplied:
            raise ValueError("OpenRouter API key is required")
        api_key = self._secret_value(supplied)
        if not api_key:
            raise ValueError("OpenRouter API key resolved to an empty value")
        endpoint = base_url.strip() or "https://openrouter.ai/api/v1"
        provider = OpenRouterProvider(
            api_key=api_key,
            model=model,
            base_url=endpoint,
            max_retries=0,
        )
        try:
            key_details = await provider.validate_key()
            models = self._openrouter_models(await provider.list_models())
        except Exception as exc:
            safe_reason = redact(str(exc), [api_key])
            raise ValueError(f"Provider was not saved: {safe_reason}") from exc
        finally:
            await provider.close()
        available_ids = {item.id for item in models}
        if model not in available_ids:
            raise ValueError(
                f"Provider was not saved: model {model!r} is not in the current OpenRouter catalog"
            )
        reference = (
            supplied
            if supplied.startswith(("env://", "file://", "keyring://"))
            else store_project_secret(self.context.root, f"{normalized}-openrouter", api_key)
        )
        rerouted = self.add(
            name=normalized,
            provider_type="openrouter",
            base_url=endpoint,
            model=model,
            api_key_reference=reference,
            make_default=make_default,
        )
        label = str(key_details.get("label") or "validated key")
        remaining = key_details.get("limit_remaining")
        detail = f"Key {label} validated"
        if remaining is not None:
            detail += f"; limit remaining {remaining}"
        return (
            ProviderStatus(
                name=normalized,
                type="openrouter",
                base_url=endpoint,
                model=model,
                connected=True,
                detail=self._routing_detail(detail, rerouted),
            ),
            models,
        )

    @staticmethod
    def _routing_detail(detail: str, rerouted: list[str]) -> str:
        if not rerouted:
            return detail
        roles = "all agent roles" if len(rerouted) == len(ModelRole) else ", ".join(rerouted)
        return f"{detail}; routed {roles} to this provider".lstrip("; ")

    async def health(self, name: str) -> ProviderStatus:
        config = self.context.settings.providers.get(name)
        if config is None:
            raise ValueError(f"Unknown provider {name}")
        provider = create_provider(name, config)
        try:
            result = await provider.health_check()
        finally:
            await provider.close()
        return ProviderStatus(
            name=name,
            type=config.type,
            base_url=config.base_url,
            model=config.model,
            connected=bool(result.get("healthy")),
            detail=str(
                result.get("error")
                or result.get("key_label")
                or f"{result.get('latency_ms', 0):.0f} ms"
            ),
        )

    async def health_all(self) -> list[ProviderStatus]:
        return await asyncio.gather(
            *(self.health(name) for name in self.context.settings.providers)
        )

    def models(self) -> list[dict[str, object]]:
        routed = {profile: role for role, profile in self.context.settings.routing.items()}
        return [
            {
                "name": name,
                "provider": profile.provider,
                "model": profile.model,
                "role": routed.get(name, ""),
                "context_window": profile.context_window,
                "cost": profile.cost_classification,
                "latency": profile.expected_latency,
                "local": profile.local,
            }
            for name, profile in self.context.settings.models.items()
        ]

    def session_profile(self, session_id: str) -> str:
        """Return the session-scoped model profile, if it is still usable."""
        with self.context.database.session() as session:
            conversation = session.get(ConversationSession, session_id)
            selected = (conversation.active_model if conversation else "") or ""
        return selected if self.route_is_usable(selected) else ""

    def select_for_session(self, session_id: str, profile_name: str) -> None:
        profile = self.context.settings.models.get(profile_name)
        if profile is None:
            raise ValueError(f"Unknown model profile {profile_name}")
        with self.context.database.session() as session:
            conversation = session.scalar(
                select(ConversationSession).where(ConversationSession.id == session_id)
            )
            if conversation is None:
                raise ValueError(f"Unknown session {session_id}")
            conversation.active_model = profile_name
        self.context.events.publish(
            ModelSelected(
                profile=profile_name,
                provider=profile.provider,
                model=profile.model,
                details={"persisted_routing": False},
            )
        )
