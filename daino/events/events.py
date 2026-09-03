"""Contracts for observable Daino activity.

These events deliberately contain serializable data and no Textual concepts so a
future web or remote Mission Control client can consume the same stream.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionEvent:
    mission_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return type(self).__name__

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["timestamp"] = self.timestamp.isoformat()
        return value


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionCreated(MissionEvent):
    request: str
    mode: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionStarted(MissionEvent):
    workspace: str = ""
    branch: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionPaused(MissionEvent):
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionCompleted(MissionEvent):
    evidence_path: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class MissionFailed(MissionEvent):
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskStarted(MissionEvent):
    task_id: str
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskCompleted(MissionEvent):
    task_id: str
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSplit(MissionEvent):
    """One planned task proved too large for its model and became several.

    Distinct from ``TaskCompleted`` because the parent did not complete: it was
    cancelled and replaced. Reporting it as a completion would tell the user the
    work was done, and reporting nothing would leave them watching one task
    silently turn into three.
    """

    task_id: str
    title: str
    reason: str
    slices: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class TodoUpdated(MissionEvent):
    """The current user-visible checklist for one conversation session."""

    session_id: str
    todos: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompacted(MissionEvent):
    before_tokens: int
    after_tokens: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentRoleChanged(MissionEvent):
    role: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSelected(MissionEvent):
    profile: str
    provider: str
    model: str
    role: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelEscalationRequested(MissionEvent):
    role: str
    reason: str
    profile: str = ""
    pinned: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelStreamChunk(MissionEvent):
    content: str
    role: str = "assistant"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReasoningChunk(MissionEvent):
    """Ephemeral provider-supplied reasoning emitted while a model is working.

    Reasoning is intentionally a separate event from ``ModelStreamChunk``: the
    latter is user-facing answer text that belongs in the conversation, while
    this event is live progress only and must never be persisted.
    """

    content: str
    role: str = "assistant"
    provider: str = ""
    model: str = ""
    profile: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamPlanned(MissionEvent):
    """A team lead settled on a roster; members carries id/role/scope per member."""

    summary: str
    members: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamMemberStarted(MissionEvent):
    member: str
    role: str
    objective: str
    scope: list[str] = field(default_factory=list)
    read_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamMemberCompleted(MissionEvent):
    member: str
    role: str
    summary: str = ""
    changed: list[str] = field(default_factory=list)
    steps: int = 0
    success: bool = True
    error: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(MissionEvent):
    tool: str
    summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolProgress(MissionEvent):
    tool: str
    summary: str
    progress: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCompleted(MissionEvent):
    tool: str
    summary: str
    duration_seconds: float = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFailed(MissionEvent):
    tool: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FileChanged(MissionEvent):
    path: str
    action: str
    #: The rendered diff for this one edit, so a client can show what changed as
    #: it happens rather than only once the whole turn finishes. Empty when the
    #: change has no textual diff, such as a binary file.
    diff: str = ""
    added: int = 0
    removed: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class TestsStarted(MissionEvent):
    commands: list[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class TestsCompleted(MissionEvent):
    passed: bool
    passed_count: int = 0
    failed_count: int = 0
    duration_seconds: float = 0
    failures: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(MissionEvent):
    category: str
    subject: str
    risk: str = "medium"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalResolved(MissionEvent):
    category: str
    approved: bool
    scope: str = "once"


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointCreated(MissionEvent):
    checkpoint_id: str
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentStarted(MissionEvent):
    target: str
    action: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentProgress(MissionEvent):
    target: str
    stage: str
    progress: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentVerified(MissionEvent):
    target: str
    healthy: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentFailed(MissionEvent):
    target: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RollbackStarted(MissionEvent):
    target: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RollbackCompleted(MissionEvent):
    target: str
    result: dict[str, Any] = field(default_factory=dict)


# --- Browser GUI events -----------------------------------------------------
# Consumed by the local FastAPI/WebSocket server; the TUI ignores unknown kinds.


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignCreated(MissionEvent):
    design_id: str
    name: str
    design_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignUpdated(MissionEvent):
    design_id: str
    name: str = ""
    design_type: str = ""
    version: int = 1
    #: Coarse description of the mutation, e.g. "add_node"/"connect"/"prototype".
    change: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceCreated(MissionEvent):
    workspace_id: str
    name: str
    kind: str = "general"
    folder: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceUpdated(MissionEvent):
    """A workspace's goal, tasks, artifacts, or sources changed."""

    workspace_id: str
    #: Coarse description of the mutation, e.g. "tasks"/"artifact"/"source".
    change: str = ""
    #: The artifact involved, repository-relative, when the change names one.
    path: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceRunUpdated(MissionEvent):
    """A workspace run started, advanced, paused, or finished.

    Carries only identifiers and a sentence: the client refetches the run to
    render it, exactly as it does for a workspace change. Streaming the whole
    run down the socket would duplicate the state that the database already
    owns and the timeline already persists.
    """

    workspace_id: str
    run_id: str
    status: str = ""
    #: The plan step this concerns, when it concerns one.
    task_id: str = ""
    #: One user-facing sentence, already fit to show.
    message: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class GitChanged(MissionEvent):
    """The working tree changed (an edit, write, or delete) so clients refresh."""

    paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreviewStarted(MissionEvent):
    url: str
    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PreviewStopped(MissionEvent):
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetWarning(MissionEvent):
    """A run has crossed the warning fraction of its configured ceiling.

    Raised once per run. Repeating it every call would turn the last third of a
    long mission into a wall of identical warnings, which is how a warning stops
    being read.
    """

    fraction: float
    message: str
    cost_usd: float = 0.0
    total_tokens: int = 0
    model_calls: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetExhausted(MissionEvent):
    """A run hit a ceiling and no further model call will be made for it."""

    dimension: str
    spent: float
    limit: float
    message: str
