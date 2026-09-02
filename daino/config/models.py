"""Typed project configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectConfig(BaseModel):
    name: str = "daino-project"
    default_mode: Literal["direct", "specification", "program"] = "specification"
    context_budget_tokens: int = 24_000


class DatabaseConfig(BaseModel):
    # Relative sqlite path resolved under the project root at connect time. The
    # ``daino.config.paths`` resolver maps this default onto a pre-existing
    # legacy ``.vasuki/vasuki.db`` when that is the only database present.
    url: str = "sqlite:///.daino/daino.db"


class RuntimeConfig(BaseModel):
    #: Docker by default so agent commands run sandboxed. ``daino init`` probes
    #: the daemon and records ``local`` instead when it cannot be reached: a
    #: container runtime the user cannot talk to fails every command, which
    #: looks like a broken agent rather than a missing sandbox.
    default: Literal["local", "docker", "ssh"] = "docker"
    network_access: Literal["restricted", "allowed"] = "restricted"
    command_timeout_seconds: int = 600
    #: The non-slim image is deliberate: it ships Git, and ``git diff --check``
    #: is the fallback verification command, so the slim image failed every
    #: check with "git: not found". One image still cannot carry every
    #: toolchain — a Node or Go project needs its own image set here.
    docker_image: str = "python:3.12"
    cpu_limit: float = 2.0
    memory_limit: str = "2g"


class VerificationConfig(BaseModel):
    repair_attempts_local: int = 2
    total_attempts: int = 4
    require_review: bool = True
    #: Run the project's discovered build/test commands once more over the whole
    #: workspace after every task finishes. Per-task checks pass in isolation, so
    #: a task that renames a symbol another task depends on can leave both green
    #: while the assembled project is broken; this final gate catches that.
    integration_gate: bool = True
    commands: list[str] = Field(default_factory=list)


class GitConfig(BaseModel):
    use_worktrees: bool = True
    auto_commit_verified_tasks: bool = True
    auto_push: bool = False


class SecurityConfig(BaseModel):
    require_approval_for_install: bool = True
    require_approval_for_network: bool = True
    require_approval_for_production: bool = True
    allowed_commands: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)


#: Capability defaults per provider type. Ollama parses tool calls itself and
#: runs offline, so native tool calling is on by default; vLLM only supports it
#: when served with a tool-call parser, so it stays opt-in there.
_DEFAULT_FEATURES: dict[str, list[str]] = {
    "openrouter": ["chat", "structured", "tools"],
    "ollama": ["chat", "structured", "tools"],
    "vllm": ["chat", "structured"],
    "openai-compatible": ["chat", "structured"],
}


class ProviderConfig(BaseModel):
    type: Literal["openrouter", "ollama", "vllm", "openai-compatible"]
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 120
    max_retries: int = 2
    context_limit: int = 32_768
    max_output_tokens: int = 16_384
    application_name: str | None = None
    referring_url: str | None = None
    features: list[str] = Field(default_factory=list)
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    #: How many generation requests may be in flight at once. ``None`` resolves
    #: to 1 for a local runtime (Ollama/vLLM serve one model, so concurrent
    #: requests queue there and each returns slower) and to unlimited for a
    #: hosted API, which answers from separate capacity. ``0`` is unlimited.
    max_concurrent_requests: int | None = Field(default=None, ge=0, le=64)

    @field_validator("api_key")
    @classmethod
    def secret_must_be_reference(cls, value: str) -> str:
        if value and not value.startswith(("env://", "keyring://", "file://")):
            raise ValueError("api_key must be a secret reference (env://, keyring://, file://)")
        return value

    @model_validator(mode="after")
    def apply_default_features(self) -> ProviderConfig:
        if not self.features:
            self.features = list(_DEFAULT_FEATURES.get(self.type, ["chat", "structured"]))
        if self.max_concurrent_requests is None:
            self.max_concurrent_requests = 1 if self.type in {"ollama", "vllm"} else 0
        return self

    @property
    def concurrency(self) -> int:
        """Resolved in-flight request limit; 0 means unlimited."""
        return self.max_concurrent_requests or 0


class ModelProfileConfig(BaseModel):
    provider: str
    model: str
    local: bool = False
    context_window: int = 32_768
    max_output_tokens: int = 16_384
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    planning_score: int = 5
    coding_score: int = 5
    debugging_score: int = 5
    review_score: int = 5
    tool_reliability: int = 5
    structured_reliability: int = 5
    frontend_capability: int = 5
    backend_capability: int = 5
    infrastructure_capability: int = 5
    cost_classification: Literal["free", "low", "medium", "high"] = "low"
    expected_latency: Literal["low", "medium", "high"] = "medium"
    data_sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    #: ``compact`` optimizes prompts and the action loop for smaller/local
    #: models. ``auto`` enables it for constrained windows or modest coding /
    #: tool reliability scores while leaving strong cloud profiles unchanged.
    execution_mode: Literal["auto", "compact", "standard"] = "auto"
    #: Optional hard ceiling for the first task packet. Zero lets Daino derive
    #: a safe value from the model window and project context budget.
    initial_context_tokens: int = Field(default=0, ge=0)
    #: Optional per-run hard ceiling. Zero leaves productive runs unlimited;
    #: repeated low-value actions are handled separately by escalation. The
    #: ceiling was capped at 100, which contradicted the advice the loop and TUI
    #: give on hitting it ("increase max_agent_steps for genuinely long tasks"):
    #: a task needing a large but finite budget could not express one. Bounded
    #: only against runaway config now — a stall still ends a run in ~12 actions.
    max_agent_steps: int = Field(default=0, ge=0, le=10_000)
    no_progress_limit: int = Field(default=3, ge=2, le=12)
    staged_retrieval: bool = True


_COST_RANK = {"free": 0, "low": 1, "medium": 2, "high": 3}


def model_strength(profile: ModelProfileConfig) -> tuple[int, int, int, int]:
    """Rank models so the strongest sorts first (use with ``reverse=True``).

    Freshly configured models all carry neutral capability scores, so the ranking
    cannot lean on those alone: a non-local model is treated as the stronger
    failover choice, then the reasoning-score aggregate, then window size, then
    cost tier as a coarse proxy for capability.
    """
    reasoning = (
        profile.planning_score
        + profile.review_score
        + profile.debugging_score
        + profile.coding_score
    )
    return (
        0 if profile.local else 1,
        reasoning,
        profile.context_window,
        _COST_RANK.get(profile.cost_classification, 1),
    )


class DeploymentAuthConfig(BaseModel):
    key_path: str | None = None
    agent: bool = True
    known_hosts: str | None = None


class DeploymentTargetConfig(BaseModel):
    type: Literal["local-docker", "ssh"]
    host: str | None = None
    port: int = 22
    username: str | None = None
    auth: DeploymentAuthConfig = Field(default_factory=DeploymentAuthConfig)
    deployment_path: str = "/opt/apps/app"
    strategy: Literal["docker-compose"] = "docker-compose"
    environment: str = "development"
    compose_file: str = "compose.yaml"
    health_url: str | None = None
    health_commands: list[str] = Field(default_factory=list)
    retain_releases: int = 5


class DeploymentConfig(BaseModel):
    targets: dict[str, DeploymentTargetConfig] = Field(default_factory=dict)


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    json_logs: bool = True
    otel_endpoint: str | None = None


class TUIConfig(BaseModel):
    theme: Literal["dark", "light", "system"] = "dark"
    display_mode: Literal["compact", "standard", "detailed"] = "standard"
    show_hints: bool = True
    streaming: bool = True
    keybindings: dict[str, str] = Field(default_factory=dict)


class NotificationsConfig(BaseModel):
    """When to raise an OS notification about a long-running turn.

    A turn the user walked away from is the normal case, so the three moments
    that need attention — it finished, it broke, it is waiting for approval —
    are announced outside the application window.
    """

    enabled: bool = True
    #: A real OS notification (osascript / notify-send / Windows balloon).
    desktop: bool = True
    #: The terminal bell, which most terminals turn into a tab badge.
    terminal_bell: bool = True
    on_completed: bool = True
    on_failed: bool = True
    on_approval: bool = True


class MemoryConfig(BaseModel):
    """Local-first memory policy; lexical retrieval works with all defaults."""

    enabled: bool = True
    auto_save: bool = True
    auto_extract: bool = True
    auto_resume: bool = False
    max_retrieved_items: int = Field(default=8, ge=1, le=100)
    max_context_tokens: int = Field(default=2_000, ge=128)
    compaction_threshold: float = Field(default=0.80, gt=0.1, le=1.0)
    embedding_provider: Literal["disabled", "local", "openai-compatible"] = "disabled"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    decay_enabled: bool = True
    decay_half_life_days: int = Field(default=180, ge=1)
    user_memory_enabled: bool = True
    failure_memory_enabled: bool = True

    @field_validator("embedding_api_key")
    @classmethod
    def embedding_secret_must_be_reference(cls, value: str) -> str:
        if value and not value.startswith(("env://", "keyring://", "file://")):
            raise ValueError(
                "embedding_api_key must be a secret reference (env://, keyring://, file://)"
            )
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAINO_",
        env_nested_delimiter="__",
        extra="ignore",
    )
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelProfileConfig] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    routing_fallbacks: dict[str, list[str]] = Field(default_factory=dict)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    #: Hold an OS sleep inhibitor while the agent works. A mission runs for
    #: minutes without input, and a host that suspends mid-turn drops the
    #: model connection and freezes commands in flight.
    keep_awake: bool = True

    @model_validator(mode="after")
    def backfill_routing_fallbacks(self) -> Settings:
        """Give every routed role a failover chain when none was configured.

        Escalation and provider failover both consult ``routing_fallbacks``; an
        empty map silently pins each role to a single model, so a stalled build
        can neither fail over a dead provider nor escalate to a stronger model.
        Older configs written before fallbacks existed therefore get a sensible
        chain here, in memory, without rewriting the file. Explicit configuration
        is respected: a non-empty map is left exactly as the operator set it.
        """
        if self.routing_fallbacks:
            return self
        usable = {
            name: profile
            for name, profile in self.models.items()
            if profile.provider in self.providers
        }
        if len(usable) < 2:
            return self
        ranked = sorted(usable, key=lambda name: model_strength(usable[name]), reverse=True)
        fallbacks: dict[str, list[str]] = {}
        for role, primary in self.routing.items():
            chain = [name for name in ranked if name != primary]
            if chain:
                fallbacks[role] = chain
        self.routing_fallbacks = fallbacks
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Serialize configuration without ever resolving secret references."""
        return self.model_dump(mode="json")
