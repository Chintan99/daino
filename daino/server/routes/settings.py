"""Settings the browser IDE can change: routing, provider, runtime, diagnostics.

Interface preferences (theme, font sizes, editor toggles) belong to the browser
and are kept there. What lives here is *project* state — which provider and
model profile each agent role uses, which runtime executes commands, whether a
command still needs approval — so it reaches the same ``Settings`` object the
agent runtime already holds and is persisted to ``.daino/config.yaml`` through
the same application services the TUI uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daino.config import config_path, save_settings
from daino.config.globals import load_global_data
from daino.exceptions import DainoError
from daino.model_router import ModelRole
from daino.observability import configure_logging
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/settings", tags=["settings"])

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SettingsPatch(BaseModel):
    """Every field is optional; only what is supplied is changed."""

    #: role name -> model profile name
    routing: dict[str, str] | None = None
    #: point every agent role at this provider's model profile
    default_provider: str | None = None
    runtime: Literal["local", "sandbox", "docker", "ssh"] | None = None
    network_access: Literal["restricted", "allowed"] | None = None
    log_level: LogLevel | None = None
    require_approval_for_install: bool | None = None
    require_approval_for_network: bool | None = None
    require_approval_for_production: bool | None = None
    require_review: bool | None = None
    keep_awake: bool | None = None
    notifications_enabled: bool | None = None
    notify_on_completed: bool | None = None
    notify_on_failed: bool | None = None
    notify_on_approval: bool | None = None
    notify_desktop: bool | None = None
    notify_terminal_bell: bool | None = None


def _provider_scopes(root: Path) -> dict[str, str]:
    """Which layer each provider actually lives in.

    Providers are merged from the user's global file and the project's own, and
    the editing form has to say which one a change will be written to — silently
    turning an inherited provider into a project pin is a nasty surprise the
    next time the global one is updated.
    """
    project_layer: dict = {}
    path = config_path(root)
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            project_layer = loaded.get("providers") or {}
    global_layer = load_global_data().get("providers") or {}
    scopes: dict[str, str] = {}
    for name in global_layer:
        scopes[name] = "global"
    for name in project_layer:
        scopes[name] = "project"  # a project override wins where both exist
    return scopes


def _payload(state: GuiState) -> dict:
    settings = state.context.settings
    scopes = _provider_scopes(state.root)
    return {
        "project": {
            "name": settings.project.name,
            "default_mode": settings.project.default_mode,
            "context_budget_tokens": settings.project.context_budget_tokens,
        },
        # Never the api_key reference: the browser has no use for it and it
        # names a secret location.
        "providers": [
            {
                "name": name,
                "type": provider.type,
                "base_url": provider.base_url,
                "model": provider.model,
                "scope": scopes.get(name, "project"),
            }
            for name, provider in settings.providers.items()
        ],
        "models": state.providers.models(),
        "roles": [role.value for role in ModelRole],
        "routing": dict(settings.routing),
        "runtime": {
            "default": settings.runtime.default,
            "network_access": settings.runtime.network_access,
            "docker_image": settings.runtime.docker_image,
            "command_timeout_seconds": settings.runtime.command_timeout_seconds,
        },
        "security": {
            "require_approval_for_install": settings.security.require_approval_for_install,
            "require_approval_for_network": settings.security.require_approval_for_network,
            "require_approval_for_production": settings.security.require_approval_for_production,
        },
        "verification": {
            "require_review": settings.verification.require_review,
            "commands": list(settings.verification.commands),
        },
        "observability": {"log_level": settings.observability.log_level},
        "keep_awake": settings.keep_awake,
        "notifications": {
            "enabled": settings.notifications.enabled,
            "desktop": settings.notifications.desktop,
            "terminal_bell": settings.notifications.terminal_bell,
            "on_completed": settings.notifications.on_completed,
            "on_failed": settings.notifications.on_failed,
            "on_approval": settings.notifications.on_approval,
        },
        "memory": {"enabled": settings.memory.enabled, "auto_save": settings.memory.auto_save},
    }


@router.get("")
def read_settings(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    return _payload(state)


@router.patch("")
def patch_settings(
    state: Annotated[GuiState, Depends(get_state)], body: SettingsPatch
) -> dict:
    settings = state.context.settings
    roles = {role.value for role in ModelRole}

    if body.default_provider is not None:
        provider = body.default_provider
        if provider not in settings.providers:
            raise HTTPException(status_code=400, detail=f"Unknown provider {provider}")
        # Prefer a profile named after the provider — that is what `daino
        # providers add` creates — and otherwise take the first profile using it.
        candidates = [
            name for name, profile in settings.models.items() if profile.provider == provider
        ]
        if not candidates:
            raise HTTPException(
                status_code=400,
                detail=f"No model profile uses provider {provider}",
            )
        profile_name = provider if provider in candidates else candidates[0]
        for role in roles:
            settings.routing[role] = profile_name

    if body.routing is not None:
        for role, profile_name in body.routing.items():
            if role not in roles:
                raise HTTPException(status_code=400, detail=f"Unknown agent role {role}")
            if profile_name not in settings.models:
                raise HTTPException(
                    status_code=400, detail=f"Unknown model profile {profile_name}"
                )
            settings.routing[role] = profile_name

    if body.runtime is not None:
        # Reuses the settings service so the GUI and the TUI validate identically.
        state.settings.set_runtime(body.runtime)
    if body.network_access is not None:
        settings.runtime.network_access = body.network_access
    if body.log_level is not None:
        settings.observability.log_level = body.log_level
        # Apply now rather than at the next start: a user who turns on DEBUG
        # while chasing a failure needs it for the run in front of them.
        configure_logging(body.log_level)
    if body.require_approval_for_install is not None:
        settings.security.require_approval_for_install = body.require_approval_for_install
    if body.require_approval_for_network is not None:
        settings.security.require_approval_for_network = body.require_approval_for_network
    if body.require_approval_for_production is not None:
        settings.security.require_approval_for_production = body.require_approval_for_production
    if body.require_review is not None:
        settings.verification.require_review = body.require_review
    if body.keep_awake is not None:
        settings.keep_awake = body.keep_awake
        # The live inhibitor honours the change immediately, including releasing
        # one that is currently held.
        state.missions.attention.keep_awake.enabled = body.keep_awake
        if not body.keep_awake:
            state.missions.attention.keep_awake.shutdown()
    for field_name, value in (
        ("enabled", body.notifications_enabled),
        ("desktop", body.notify_desktop),
        ("terminal_bell", body.notify_terminal_bell),
        ("on_completed", body.notify_on_completed),
        ("on_failed", body.notify_on_failed),
        ("on_approval", body.notify_on_approval),
    ):
        if value is not None:
            setattr(settings.notifications, field_name, value)
    # The service holds the same config object, so it needs no reload.
    state.missions.attention.notifications.config = settings.notifications

    save_settings(settings, state.root)
    return _payload(state)


@router.post("/reload")
def reload_settings(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Re-read configuration from disk, picking up an edit made outside the IDE."""
    state.settings.reload()
    return _payload(state)


class ProviderForm(BaseModel):
    """One provider as the GUI's form describes it.

    ``api_key`` may be a literal the user just typed or a reference
    (``env://`` / ``file://`` / ``keyring://``). Literals are stored through the
    project or global secret store and only the reference is written to YAML —
    the same path ``daino providers add`` takes.
    """

    name: str = ""
    type: Literal["openrouter", "ollama", "vllm", "openai-compatible"]
    base_url: str
    model: str = ""
    api_key: str = ""
    scope: Literal["project", "global"] = "project"
    make_default: bool = True


def _status(item) -> dict:  # noqa: ANN001 - ProviderStatus view model
    return {
        "name": item.name,
        "type": item.type,
        "base_url": item.base_url,
        "model": item.model,
        "connected": item.connected,
        "detail": item.detail,
    }


@router.post("/providers")
async def save_provider(
    state: Annotated[GuiState, Depends(get_state)], body: ProviderForm
) -> dict:
    """Create or update a provider.

    OpenRouter is validated *before* anything is written — the key is checked and
    the model must exist in the live catalog — because a bad key there is silent
    until the first turn. The self-hosted types are saved and then health-checked,
    and the result is reported to the caller: a local Ollama that is simply not
    running yet should still be configurable.
    """
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Provider name is required")
    try:
        status, models = await state.providers.configure(
            name=body.name,
            provider_type=body.type,
            base_url=body.base_url,
            model=body.model,
            api_key_input=body.api_key,
            make_default=body.make_default,
            scope=body.scope,
        )
    except (ValueError, DainoError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "provider": _status(status),
        "catalog": [
            {"id": item.id, "name": item.name, "detail": ""} for item in models
        ],
        "settings": _payload(state),
    }


@router.post("/providers/test")
async def test_provider(
    state: Annotated[GuiState, Depends(get_state)], body: ProviderForm
) -> dict:
    """Test a provider form end to end without saving anything.

    Returns one entry per step — endpoint, credentials, model, generation — so a
    failure names itself. The generation step sends a single one-token request:
    it is the only check that proves an agent turn would actually work.
    """
    try:
        diagnosis = await state.providers.diagnose(
            name=body.name,
            provider_type=body.type,
            base_url=body.base_url,
            model=body.model,
            api_key_input=body.api_key,
        )
    except (ValueError, DainoError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a test reports, it does not fail
        return {
            "provider": {
                "name": body.name or body.type,
                "type": body.type,
                "base_url": body.base_url,
                "model": body.model,
                "connected": False,
                "detail": str(exc),
            },
            "checks": [{"name": "endpoint", "status": "fail", "detail": str(exc)}],
        }
    return {
        "provider": _status(diagnosis.status),
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail}
            for check in diagnosis.checks
        ],
    }


@router.post("/providers/catalog")
async def provider_catalog(
    state: Annotated[GuiState, Depends(get_state)], body: ProviderForm
) -> dict:
    """List the models this provider actually offers.

    A near-miss model id fails only at the first request, so the form offers the
    real catalog instead of a free-text field wherever the provider exposes one.
    """
    try:
        if body.type == "openrouter":
            items = await state.providers.openrouter_models(
                api_key_input=body.api_key
                or (state.context.settings.providers.get(body.name.strip()).api_key
                    if body.name.strip() in state.context.settings.providers
                    else ""),
                base_url=body.base_url,
            )
            return {
                "models": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "detail": f"{item.context_length:,} ctx" if item.context_length else "",
                    }
                    for item in items
                ]
            }
        if body.type == "ollama":
            items = await state.providers.ollama_models(base_url=body.base_url)
            return {
                "models": [
                    {"id": item.id, "name": item.name, "detail": item.detail} for item in items
                ]
            }
    except (ValueError, DainoError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is a 400 here
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # vLLM and generic OpenAI-compatible gateways have no standard catalog.
    return {"models": []}


@router.get("/providers/health")
async def provider_health(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Probe every configured provider. Slow by nature — it makes real calls."""
    results = await state.providers.health_all()
    return {
        "providers": [
            {
                "name": item.name,
                "type": item.type,
                "base_url": item.base_url,
                "model": item.model,
                "connected": item.connected,
                "detail": item.detail,
            }
            for item in results
        ]
    }
