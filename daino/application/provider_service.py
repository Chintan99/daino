"""Provider and session-scoped model selection."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from daino.application.context import ProjectContext
from daino.application.view_models import (
    CatalogModel,
    OpenRouterModel,
    ProviderCheck,
    ProviderDiagnosis,
    ProviderStatus,
)
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
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings, model_strength
from daino.events import ModelSelected
from daino.model_router import ModelRole
from daino.persistence.models import ConversationSession, Provider
from daino.providers import OllamaProvider, OpenRouterProvider, create_provider
from daino.providers.factory import build_provider
from daino.schemas import Message
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
        """Point agent roles at ``profile_name`` and keep failover fallbacks current.

        Routes that no longer resolve to a configured provider are always repaired;
        otherwise the profile only takes over every role when it is made the default.
        Without this a provider connected after the first one is validated, saved,
        and then never used, because every role still points at the original.

        Escalation and provider failover both need somewhere to go: a role with no
        fallbacks silently pins to one model, so a stalled build can neither fail
        over a dead provider nor escalate to a stronger model. Every role's
        fallbacks are therefore recomputed here from the other usable models,
        strongest first, whenever routing changes.
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
        self._apply_fallbacks(selected_settings)
        return rerouted

    @staticmethod
    def _apply_fallbacks(settings: Settings) -> None:
        """Set each role's failover chain to the other usable models, strongest first.

        Fallbacks are for a different failure than routing: routing picks the model
        for the job, fallbacks are where the gateway turns when that model's
        provider is unavailable or the router escalates a stalled task. Leaving
        them empty is what made escalation a no-op on a two-model install.
        """
        usable = {
            name: profile
            for name, profile in settings.models.items()
            if profile.provider in settings.providers
        }
        ranked = sorted(usable, key=lambda name: model_strength(usable[name]), reverse=True)
        fallbacks: dict[str, list[str]] = {}
        for role in ModelRole:
            primary = settings.routing.get(role.value)
            chain = [name for name in ranked if name != primary]
            if chain:
                fallbacks[role.value] = chain
        settings.routing_fallbacks = fallbacks

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
        context_window: int = 0,
    ) -> list[str]:
        """Add or update a provider without accepting literal secret values.

        ``context_window`` is the model's real input window when the provider's
        catalog reports one. Leaving it at zero keeps
        :class:`ModelProfileConfig`'s conservative default, which is the right
        answer for a provider that cannot say — and the wrong one for a model
        that can: every budget in the agent is derived from this number, so an
        understated window makes the loop compact a transcript that would have
        fitted, drop context it then has to re-read, and spend several times the
        tokens the work needed.

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
        profile_fields: dict[str, Any] = {
            "provider": normalized,
            "model": provider.model,
            "local": local,
        }
        if context_window > 0:
            profile_fields["context_window"] = context_window
        target_settings.models[normalized] = ModelProfileConfig(**profile_fields)
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
        # The catalog was just fetched to validate the model id, and it carries
        # the model's real context length. Using it is the difference between
        # budgeting against the truth and budgeting against a default.
        chosen = next((item for item in models if item.id == model), None)
        rerouted = self.add(
            name=normalized,
            provider_type="openrouter",
            base_url=endpoint,
            model=model,
            api_key_reference=reference,
            make_default=make_default,
            scope=scope,
            context_window=chosen.context_length if chosen else 0,
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

    #: Per-step time budgets for a diagnosis. A local model that has to be
    #: loaded from disk is slow the first time, so generation gets the most.
    _REACH_TIMEOUT = 10.0
    _AUTH_TIMEOUT = 12.0
    _CATALOG_TIMEOUT = 15.0
    _GENERATE_TIMEOUT = 60.0

    async def _catalog_ids(self, provider: object, provider_type: str) -> list[str] | None:
        """Model ids this provider says it offers, or ``None`` when it has no list."""
        try:
            if provider_type == "ollama":
                return [item.id for item in self._ollama_models(await provider.list_models())]
            if provider_type == "openrouter":
                return [item.id for item in self._openrouter_models(await provider.list_models())]
            # vLLM and generic gateways expose the OpenAI /models endpoint.
            response = await provider.client.get("models")  # type: ignore[attr-defined]
            if response.status_code != 200:
                return None
            data = response.json().get("data")
            if not isinstance(data, list):
                return None
            return [
                str(item.get("id"))
                for item in data
                if isinstance(item, dict) and item.get("id")
            ]
        except Exception:  # noqa: BLE001 - absence of a catalog is not a failure
            return None

    async def diagnose(
        self,
        *,
        name: str = "",
        provider_type: str,
        base_url: str,
        model: str,
        api_key_input: str = "",
    ) -> ProviderDiagnosis:
        """Test a provider configuration end to end, without saving it.

        Four questions, answered separately, because each one fails for its own
        reason: does the endpoint answer, is the credential accepted, is *this*
        model actually available there, and can it generate? The last is the only
        one that proves the configuration would work in a turn — it sends a
        one-token request — and the earlier ones say where it broke when it does.
        """
        existing = self.context.settings.providers.get(name.strip())
        supplied = api_key_input.strip() or (existing.api_key if existing else "")
        key = self._secret_value(supplied)
        secrets = [key] if key else []
        config = ProviderConfig(
            type=provider_type,  # type: ignore[arg-type]
            base_url=base_url.strip(),
            model=model.strip(),
            api_key="",
            timeout=self._GENERATE_TIMEOUT,
            max_retries=0,
        )
        provider = build_provider(name.strip() or provider_type, config, api_key=key)
        checks: list[ProviderCheck] = []

        def record(step: str, status: str, detail: str = "") -> None:
            checks.append(ProviderCheck(name=step, status=status, detail=redact(detail, secrets)))

        try:
            # 1. Does anything answer at this URL?
            try:
                result = await asyncio.wait_for(
                    provider.health_check(), timeout=self._REACH_TIMEOUT
                )
            except TimeoutError:
                record("endpoint", "fail", f"no response within {self._REACH_TIMEOUT:.0f}s")
                result = {"healthy": False}
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                record("endpoint", "fail", str(exc))
                result = {"healthy": False}
            else:
                if result.get("healthy"):
                    record(
                        "endpoint",
                        "pass",
                        f"answered in {float(result.get('latency_ms', 0)):.0f} ms",
                    )
                else:
                    record("endpoint", "fail", str(result.get("error") or "unreachable"))

            reachable = bool(result.get("healthy"))

            # 2. Is the credential accepted? Only OpenRouter can be asked directly.
            if not reachable:
                record("credentials", "skip", "endpoint did not answer")
            elif provider_type == "openrouter":
                try:
                    details = await asyncio.wait_for(
                        provider.validate_key(), timeout=self._AUTH_TIMEOUT  # type: ignore[attr-defined]
                    )
                except TimeoutError:
                    record("credentials", "fail", f"no response within {self._AUTH_TIMEOUT:.0f}s")
                except Exception as exc:  # noqa: BLE001 - a rejected key is a result
                    record("credentials", "fail", str(exc))
                else:
                    label = str(details.get("label") or "validated")
                    remaining = details.get("limit_remaining")
                    suffix = f"; limit remaining {remaining}" if remaining is not None else ""
                    record("credentials", "pass", f"key {label}{suffix}")
            elif key:
                record("credentials", "skip", "sent with each request; covered by generation")
            else:
                record("credentials", "skip", "no key configured")

            # 3. Is this exact model available there?
            catalog: list[str] | None = None
            if not reachable:
                record("model", "skip", "endpoint did not answer")
            elif not config.model:
                record("model", "fail", "no model selected")
            else:
                try:
                    catalog = await asyncio.wait_for(
                        self._catalog_ids(provider, provider_type), timeout=self._CATALOG_TIMEOUT
                    )
                except TimeoutError:
                    catalog = None
                if catalog is None:
                    record("model", "skip", "this provider publishes no model list")
                elif config.model in catalog:
                    record("model", "pass", f"{config.model} is available")
                else:
                    stem = config.model.split(":", 1)[0]
                    near = [item for item in catalog if item.split(":", 1)[0] == stem]
                    hint = f"; did you mean {near[0]}?" if near else ""
                    record(
                        "model",
                        "fail",
                        f"{config.model} is not among the {len(catalog)} models offered{hint}",
                    )

            # 4. Can it actually generate? The only check that proves a turn would work.
            if not reachable or not config.model:
                record("generation", "skip", "nothing to send a request to")
            else:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        provider.complete(  # type: ignore[attr-defined]
                            [Message(role="user", content="ping")], max_tokens=1
                        ),
                        timeout=self._GENERATE_TIMEOUT,
                    )
                except TimeoutError:
                    record(
                        "generation",
                        "fail",
                        f"no reply within {self._GENERATE_TIMEOUT:.0f}s"
                        " (a local model may still be loading)",
                    )
                except Exception as exc:  # noqa: BLE001 - the point is to report it
                    record("generation", "fail", str(exc))
                else:
                    record(
                        "generation",
                        "pass",
                        f"replied in {(time.monotonic() - started) * 1000:.0f} ms",
                    )
        finally:
            await provider.close()

        failed = [check for check in checks if check.status == "fail"]
        detail = failed[0].detail if failed else "all checks passed"
        return ProviderDiagnosis(
            status=ProviderStatus(
                name=name.strip() or provider_type,
                type=provider_type,
                base_url=config.base_url,
                model=config.model,
                connected=not failed,
                detail=redact(detail, secrets),
            ),
            checks=tuple(checks),
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

    def unpin_session(self, session_id: str) -> None:
        """Clear a session's model pin so routing (and escalation) applies again.

        A pinned session is deliberately excluded from escalation — the user
        asked for *that* model — so leaving one pinned by default silently
        disables the recovery path when a weaker model stalls.
        """
        with self.context.database.session() as session:
            conversation = session.scalar(
                select(ConversationSession).where(ConversationSession.id == session_id)
            )
            if conversation is None:
                raise ValueError(f"Unknown session {session_id}")
            conversation.active_model = ""

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
