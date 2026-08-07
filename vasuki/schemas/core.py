"""Validated contracts shared by providers, agents, tools, and persistence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base contract that rejects unknown model output fields."""

    model_config = ConfigDict(extra="forbid")


class ToolCall(StrictModel):
    """One native function call requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    #: Native tool calls attached to an assistant message.
    tool_calls: list[ToolCall] = Field(default_factory=list)
    #: Identifies which tool call a ``tool`` message answers.
    tool_call_id: str = ""


class LLMResponse(StrictModel):
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class ProjectMode(StrEnum):
    DIRECT = "direct"
    SPECIFICATION = "specification"
    PROGRAM = "program"


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CHANGE_APPROVAL = "awaiting_change_approval"
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
    action: Literal["create", "patch", "replace", "delete"]
    unified_diff: str | None = None
    content: str | None = None
    #: For ``replace``: the exact text to find. Must occur once unless
    #: ``replace_all`` is set. Preferred over ``unified_diff`` — an anchor string
    #: either matches or it does not, with no line numbers or context to drift.
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    reason: str


class Implementation(StrictModel):
    summary: str
    modifications: list[FileModification]
    verification_commands: list[str] = Field(default_factory=list)


class EditSpec(StrictModel):
    """One exact-span replacement inside a ``multi_edit``."""

    old_string: str
    new_string: str
    replace_all: bool = False


class TodoItem(StrictModel):
    """One step of the agent's plan for the current request."""

    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class AgentAction(StrictModel):
    """One step an implementing agent chooses to take.

    Deliberately flat rather than a discriminated union: a single object with an
    action enum is the shape that survives every provider's structured-output
    support, from OpenRouter through local vLLM and Ollama.
    """

    thought: str
    action: Literal[
        "read_file",
        "search_text",
        "list_directory",
        "replace",
        "write",
        "delete",
        "multi_edit",
        "run_command",
        "glob",
        "grep",
        "todo",
        "respond",
        "finish",
    ]
    path: str = ""
    query: str = ""
    old_string: str = ""
    new_string: str = ""
    replace_all: bool = False
    content: str = ""
    #: For ``run_command``: the executable and its arguments. Runs without a
    #: shell, so pipes and redirects are not available.
    command: str = ""
    timeout: int = 0
    #: For ``glob``: a path pattern such as ``src/**/*.py``.
    pattern: str = ""
    #: For ``read_file``: read a window of a large file instead of the head.
    offset: int = 0
    limit: int = 0
    #: For ``multi_edit``: several replacements applied to one file in order.
    edits: list[EditSpec] = Field(default_factory=list)
    #: For ``todo``: the current plan, replaced in full each time.
    todos: list[TodoItem] = Field(default_factory=list)
    summary: str = ""
    #: For ``respond``: the answer to show the user. Kept separate from
    #: ``summary``, which means "what you changed" and is empty when the agent
    #: only answered.
    message: str = ""
    verification_commands: list[str] = Field(default_factory=list)


class AgentObservation(StrictModel):
    """The result of an action, fed back to the agent before its next step."""

    action: str
    success: bool
    detail: str


class DiffLine(StrictModel):
    """One rendered line of a file diff."""

    #: " " context, "-" removed, "+" added.
    marker: Literal[" ", "-", "+"]
    #: Line number in the file after the edit, or before it for removed lines.
    number: int
    text: str


class FileDiff(StrictModel):
    """What changed in one file, ready to render without re-reading the disk."""

    path: str
    #: "created", "modified", or "deleted".
    change: Literal["created", "modified", "deleted"]
    added: int = 0
    removed: int = 0
    lines: list[DiffLine] = Field(default_factory=list)
    #: Set when the file has no textual diff to show, such as a binary file.
    note: str = ""


class ChatOutcome(StrictModel):
    """The result of one chat-agent turn, ready to render in the transcript."""

    mission_id: str = ""
    #: Set when the agent answered instead of editing.
    answer: str = ""
    #: Set when the agent edited; what it says it did.
    summary: str = ""
    diffs: list[FileDiff] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    steps: int = 0
    #: None when no verification ran, otherwise whether every check passed.
    verified: bool | None = None
    verification_summary: str = ""


#: Roles a team member may take. Deliberately every routed role except
#: ``deployer``: a sub-agent spawned from a chat instruction must never reach the
#: deployment path, which has its own approval gates. Kept in sync with
#: ``ModelRole`` by ``test_team_member_roles_track_model_roles``.
TeamMemberRole = Literal[
    "architect",
    "planner",
    "builder",
    "reviewer",
    "debugger",
    "tester",
    "summarizer",
]


class TeamMember(StrictModel):
    """One sub-agent in a team, with the scope it is allowed to touch."""

    id: str
    role: TeamMemberRole
    objective: str
    #: Repository-relative paths or glob patterns this member may modify. Empty
    #: is only valid for a read-only member; a writer with no scope could touch
    #: the whole repository and could not be checked against its peers.
    scope: list[str] = Field(default_factory=list)
    #: Read-only members are the explorers. They fan out widest, so they are the
    #: ones that must be unable to write.
    read_only: bool = False
    #: Ids of members that must finish before this one starts.
    dependencies: list[str] = Field(default_factory=list)


class TeamPlan(StrictModel):
    """The roster a team lead proposes for one instruction."""

    summary: str
    members: list[TeamMember]


class TeamMemberOutcome(StrictModel):
    """What one member did, reported back to the chat transcript."""

    id: str
    role: str
    objective: str
    summary: str
    changed: list[str] = Field(default_factory=list)
    steps: int = 0
    success: bool = True
    error: str = ""


class TeamOutcome(StrictModel):
    plan: TeamPlan
    members: list[TeamMemberOutcome] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    #: The mission the run was recorded under, so a caller can reach its
    #: workspace, diff, and checkpoints afterwards.
    mission_id: str = ""


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
    result: CommandResult | None = None
    skipped: bool = False
    skip_reason: str = ""


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
