"""Capability-aware model selection with auditable escalation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vasuki.config.models import ModelProfileConfig, Settings
from vasuki.exceptions import ConfigurationError


class ModelRole(StrEnum):
    ARCHITECT = "architect"
    PLANNER = "planner"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"
    TESTER = "tester"
    SUMMARIZER = "summarizer"
    DEPLOYER = "deployer"


@dataclass(frozen=True)
class RoutingContext:
    failed_attempts: int = 0
    affected_files: int = 0
    architecture_change: bool = False
    security_critical: bool = False
    structured_failures: int = 0
    tests_failing: bool = False
    data_sensitivity: str = "internal"


@dataclass(frozen=True)
class ModelSelection:
    profile_name: str
    profile: ModelProfileConfig
    reason: str
    escalated: bool = False


_SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


class ModelRouter:
    """Selects a configured model for an agent role and records why."""

    def __init__(self, settings: Settings, *, file_escalation_threshold: int = 8) -> None:
        self.settings = settings
        self.file_escalation_threshold = file_escalation_threshold

    def select(
        self,
        role: ModelRole | str,
        context: RoutingContext | None = None,
        *,
        profile_override: str | None = None,
    ) -> ModelSelection:
        role_name = ModelRole(role).value
        if profile_override:
            profile = self.settings.models.get(profile_override)
            if profile is None:
                raise ConfigurationError(f"Unknown model profile {profile_override}")
            return ModelSelection(
                profile_name=profile_override,
                profile=profile,
                reason=f"Explicitly selected for {role_name} in this session",
            )
        profile_name = self.settings.routing.get(role_name)
        if not profile_name:
            raise ConfigurationError(f"No model route configured for role {role_name}")
        context = context or RoutingContext()
        triggers: list[str] = []
        if context.failed_attempts >= 2:
            triggers.append("same task failed twice")
        if context.architecture_change:
            triggers.append("architecture change required")
        if context.affected_files > self.file_escalation_threshold:
            triggers.append(f"{context.affected_files} files exceed escalation threshold")
        if context.security_critical:
            triggers.append("security-critical code")
        if context.structured_failures >= 2:
            triggers.append("repeated structured-output failures")
        if context.tests_failing and context.failed_attempts >= 2:
            triggers.append("tests still fail after local repairs")

        candidates = [profile_name]
        if triggers:
            candidates.extend(self.settings.routing_fallbacks.get(role_name, []))
            debugger = self.settings.routing.get(ModelRole.DEBUGGER.value)
            if debugger and debugger not in candidates:
                candidates.append(debugger)

        selected_name = candidates[-1] if triggers and len(candidates) > 1 else candidates[0]
        if selected_name not in self.settings.models:
            raise ConfigurationError(f"Route references unknown model profile {selected_name}")
        profile = self.settings.models[selected_name]
        requested = _SENSITIVITY.get(context.data_sensitivity, 1)
        allowed = _SENSITIVITY.get(profile.data_sensitivity, 1)
        if requested > allowed and not profile.local:
            local = next(
                (
                    (name, item)
                    for name, item in self.settings.models.items()
                    if item.local and _SENSITIVITY.get(item.data_sensitivity, 1) >= requested
                ),
                None,
            )
            if local is None:
                raise ConfigurationError("No model is allowed for the requested data sensitivity")
            selected_name, profile = local
            triggers.append("data sensitivity requires local processing")

        reason = (
            f"Escalated {role_name}: " + "; ".join(triggers)
            if triggers
            else f"Configured primary model for {role_name}"
        )
        return ModelSelection(
            profile_name=selected_name,
            profile=profile,
            reason=reason,
            escalated=selected_name != profile_name,
        )
