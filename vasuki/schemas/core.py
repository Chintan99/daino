"""Validated contracts shared by providers, agents, tools, and persistence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base contract that rejects unknown model output fields."""

    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class LLMResponse(StrictModel):
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProjectMode(StrEnum):
    DIRECT = "direct"
    SPECIFICATION = "specification"
    PROGRAM = "program"


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RequirementSpec(StrictModel):
    problem_statement: str
    goals: list[str]
    non_goals: list[str] = Field(default_factory=list)
    functional_requirements: list[str]
    non_functional_requirements: list[str] = Field(default_factory=list)
    user_roles: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    api_contracts: list[str] = Field(default_factory=list)
    data_contracts: list[str] = Field(default_factory=list)
    security_constraints: list[str] = Field(default_factory=list)
    deployment_impact: str = "none"
    acceptance_criteria: list[str]
    test_strategy: list[str]
    assumptions: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)


class TaskSpec(StrictModel):
    id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    expected_files: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    relevant_symbols: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    verification_commands: list[str]
    rollback_notes: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_model: str | None = None
    attempt_count: int = 0
    evidence: list[str] = Field(default_factory=list)


class TaskPlan(StrictModel):
    summary: str
    mode: ProjectMode
    tasks: list[TaskSpec]


class FileModification(StrictModel):
    path: str
    action: Literal["create", "patch", "delete"]
    unified_diff: str | None = None
    content: str | None = None
    reason: str


class Implementation(StrictModel):
    summary: str
    modifications: list[FileModification]
    verification_commands: list[str] = Field(default_factory=list)


class ReviewFinding(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str
    message: str
    file: str | None = None
    line: int | None = None


class ReviewReport(StrictModel):
    approved: bool
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)


class FailureReport(StrictModel):
    failure_type: str
    command: str
    summary: str
    file: str | None = None
    line: int | None = None
    likely_correction_area: str | None = None
    output_excerpt: str = ""


class CommandResult(StrictModel):
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    redacted: bool = True

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ToolResult(StrictModel):
    tool: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0


class VerificationCheck(StrictModel):
    name: str
    command: str
    passed: bool
    result: CommandResult


class VerificationReport(StrictModel):
    passed: bool
    checks: list[VerificationCheck]
    failures: list[FailureReport] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


class RepositorySymbol(StrictModel):
    name: str
    kind: str
    path: str
    line: int
    signature: str | None = None


class RepositoryFile(StrictModel):
    path: str
    language: str
    size: int
    digest: str
    summary: str
    imports: list[str] = Field(default_factory=list)
    symbols: list[RepositorySymbol] = Field(default_factory=list)


class RepositoryIndex(StrictModel):
    root: str
    generated_at: datetime
    files: list[RepositoryFile]
    languages: dict[str, int]
    frameworks: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)


class ContextBundle(StrictModel):
    task: str
    acceptance_criteria: list[str]
    architecture_decisions: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    tests: dict[str, str] = Field(default_factory=dict)
    failure_summary: str | None = None
    token_estimate: int = 0
    included_paths: list[str] = Field(default_factory=list)


class DeploymentRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DeploymentPlan(StrictModel):
    target: str
    detected_environment: dict[str, Any]
    deployment_strategy: str
    required_changes: list[str]
    files_to_upload: list[str]
    images: list[str] = Field(default_factory=list)
    persistent_volumes: list[str] = Field(default_factory=list)
    environment_variables: list[str] = Field(default_factory=list)
    database_migrations: list[str] = Field(default_factory=list)
    port_changes: list[str] = Field(default_factory=list)
    reverse_proxy_changes: list[str] = Field(default_factory=list)
    tls_requirements: list[str] = Field(default_factory=list)
    health_checks: list[str]
    risk_level: DeploymentRisk
    destructive_actions: list[str] = Field(default_factory=list)
    rollback_strategy: list[str]
