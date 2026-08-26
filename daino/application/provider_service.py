"""Provider and session-scoped model selection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from daino.application.context import ProjectContext
from daino.application.view_models import CatalogModel, OpenRouterModel, ProviderStatus
from daino.config import (
    default_settings,
    load_settings,
    save_settings,
    use_global_provider_settings,
)
from daino.config.globals import (
    has_global_provider,
    load_global_data,
    merge_layers,
    save_global,
)
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings
from daino.events import ModelSelected
from daino.model_router import ModelRole
from daino.persistence.models import ConversationSession, Provider
from daino.providers import OllamaProvider, OpenRouterProvider, create_provider
from daino.security import redact, resolve_secret, store_global_secret, store_project_secret
from daino.utils.ids import new_id


async def list_ollama_models(
    base_url: str = "http://127.0.0.1:11434/v1",
) -> list[CatalogModel]:
    """List the models a local Ollama already has pulled.

    Offering these instead of a free-text field is the difference between
    choosing a model and guessing its exact tag: ``qwen3.8:27b-mlx`` is not a
    name anyone recalls, and a near miss fails only at the first request. Kept
    at module level because onboarding needs it before a project context exists.
    """
    provider = OllamaProvider(
        model="",
        base_url=base_url.strip() or "http://127.0.0.1:11434/v1",
        max_retries=0,
    )
    try:
        return ProviderApplicationService._ollama_models(await provider.list_models())
    finally:
        await provider.close()


def _human_size(value: object) -> str:
    """Render a byte count the way a model listing should read: "16.9 GB"."""
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit not in {"B", "KB"} else f"{size:.0f} {unit}"
        size /= 1024
    return ""


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

    def _apply_routing(
        self,
        profile_name: str,
        *,
        make_default: bool,
        settings: Settings | None = None,
    ) -> list[str]:
        """Point agent roles at ``profile_name``.

        Routes that no longer resolve to a configured provider are always repaired;
        otherwise the profile only takes over every role when it is made the default.
        Without this a provider connected after the first one is validated, saved,
        and then never used, because every role still points at the original.
        """
        selected_settings = settings or self.context.settings
        routing = selected_settings.routing
        rerouted: list[str] = []
        for role in ModelRole:
            current = routing.get(role.value)
            current_profile = selected_settings.models.get(str(current)) if current else None
            current_is_usable = (
                current_profile is not None
                and current_profile.provider in selected_settings.providers
            )
            if not make_default and current_is_usable:
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
        scope: str = "project",
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
        if scope == "global":
            base = default_settings(self.context.root).safe_dump()
            target_settings = Settings.model_validate(merge_layers(base, load_global_data()))
        elif scope == "project":
            target_settings = self.context.settings
        else:
            raise ValueError(f"Unknown provider scope {scope}")
        target_settings.providers[normalized] = provider
        target_settings.models[normalized] = ModelProfileConfig(
            provider=normalized,
            model=provider.model,
            local=local,
        )
        rerouted = self._apply_routing(
            normalized,
            make_default=make_default,
            settings=target_settings,
        )
        if scope == "global":
            save_global(target_settings)
            effective = load_settings(self.context.root)
            current = self.context.settings
            for field_name in type(current).model_fields:
                setattr(current, field_name, getattr(effective, field_name))
        else:
            save_settings(target_settings, self.context.root)
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
    def _ollama_models(items: list[dict[str, Any]]) -> list[CatalogModel]:
        """Shape ``/api/tags`` (or ``/v1/models``) entries for the model picker."""
        models: list[CatalogModel] = []
        for item in items:
            model_id = str(item.get("model") or item.get("name") or item.get("id") or "").strip()
            if not model_id:
                continue
            details = item.get("details")
            details = details if isinstance(details, dict) else {}
            capabilities = item.get("capabilities")
            capabilities = capabilities if isinstance(capabilities, list) else []
            parts = [
                _human_size(item.get("size")),
                str(details.get("parameter_size") or "").strip(),
                str(details.get("quantization_level") or "").strip(),
                ", ".join(str(value) for value in capabilities if value),
            ]
            models.append(
                CatalogModel(
                    id=model_id,
                    name=model_id,
                    detail=" · ".join(part for part in parts if part),
                )
            )
        return sorted(models, key=lambda item: item.id.casefold())

    async def ollama_models(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
    ) -> list[CatalogModel]:
        """List the models a local Ollama already has pulled."""
        return await list_ollama_models(base_url)

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
        scope: str = "project",
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
                scope=scope,
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
            else (
                store_global_secret(f"{normalized}-openrouter", api_key)
                if scope == "global"
                else store_project_secret(self.context.root, f"{normalized}-openrouter", api_key)
            )
        )
        rerouted = self.add(
            name=normalized,
            provider_type="openrouter",
            base_url=endpoint,
            model=model,
            api_key_reference=reference,
            make_default=make_default,
            scope=scope,
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

    def use_global(self) -> str:
        """Drop project provider/model routes and inherit the shared configuration."""
        if not has_global_provider():
            raise ValueError("No global provider is configured. Use /globalprovider first.")
        inherited = use_global_provider_settings(self.context.root)
        # Keep the Settings object identity because gateways and mission services
        # retain it; replacing only context.settings would leave them stale.
        current = self.context.settings
        for field_name in type(current).model_fields:
            setattr(current, field_name, getattr(inherited, field_name))
        profile = self.routable_profile()
        if not profile:
            raise ValueError("Global settings do not contain a usable model route")
        return profile

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

    def set_session_effort(self, session_id: str, effort: str) -> tuple[str, str]:
        """Set reasoning effort on the session-selected profile without persisting it."""
        allowed = {
            "auto",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }
        normalized = effort.strip().casefold()
        if normalized not in allowed:
            raise ValueError(
                "Effort must be auto, none, minimal, low, medium, high, xhigh, or max."
            )
        profile_name = self.session_profile(session_id) or self.routable_profile()
        if not profile_name:
            raise ValueError("No model is selected. Configure a provider first.")
        profile = self.context.settings.models[profile_name]
        provider = self.context.settings.providers.get(profile.provider)
        if provider is None:
            raise ValueError(f"Profile {profile_name} references a missing provider")
        if provider.type == "ollama" and normalized not in {
            "auto",
            "none",
            "low",
            "medium",
            "high",
            "max",
        }:
            raise ValueError(
                "Ollama effort must be auto, none, low, medium, high, or max."
            )
        if (
            provider.type not in {"openrouter", "openai-compatible", "ollama"}
            and normalized != "auto"
        ):
            raise ValueError(
                f"{provider.type} does not expose standardized reasoning effort control."
            )
        if normalized == "max" and provider.type not in {"openrouter", "ollama"}:
            raise ValueError("The max effort level is supported through OpenRouter or Ollama.")
        profile.reasoning_effort = (  # type: ignore[assignment]
            None if normalized == "auto" else normalized
        )
        return profile_name, normalized

    def session_effort(self, session_id: str) -> str:
        profile_name = self.session_profile(session_id) or self.routable_profile()
        if not profile_name:
            return "auto"
        return self.context.settings.models[profile_name].reasoning_effort or "auto"
