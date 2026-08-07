"""Typed project configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectConfig(BaseModel):
    name: str = "vasuki-project"
    default_mode: Literal["direct", "specification", "program"] = "specification"
    context_budget_tokens: int = 24_000


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///.vasuki/vasuki.db"


class RuntimeConfig(BaseModel):
    #: Chosen by ``vasuki init`` from what the machine can actually run. The
    #: fallback is local rather than docker: a container runtime the user cannot
    #: reach fails every command, which looks like a broken agent.
    default: Literal["local", "docker", "ssh"] = "local"
    network_access: Literal["restricted", "allowed"] = "restricted"
    command_timeout_seconds: int = 600
    docker_image: str = "python:3.12-slim"
    cpu_limit: float = 2.0
    memory_limit: str = "2g"


class VerificationConfig(BaseModel):
    repair_attempts_local: int = 2
    total_attempts: int = 4
    require_review: bool = True
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
        return self


class ModelProfileConfig(BaseModel):
    provider: str
    model: str
    local: bool = False
    context_window: int = 32_768
    max_output_tokens: int = 16_384
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VASUKI_",
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

    def safe_dump(self) -> dict[str, Any]:
        """Serialize configuration without ever resolving secret references."""
        return self.model_dump(mode="json")
