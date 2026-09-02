"""Contracts for the Workspace tab.

Mirrors :mod:`daino.design.models`: Pydantic on the wire, permissive metadata
bags so a new field needs no migration. The distinction from designs is that a
workspace's documents are ordinary files in the repository rather than nodes in
one JSON blob, so an :class:`Artifact` is a description of a path, not a
container of content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Shares ``TodoItem``'s vocabulary so existing renderers apply unchanged. No
#: value here is terminal: a workspace task can be reopened.
TaskStatus = Literal["pending", "in_progress", "completed", "failed"]

WorkspaceStatus = Literal["active", "archived"]

#: Where a run is. ``waiting_for_user`` and ``waiting_for_approval`` are held
#: states, not failures: the executor is alive and the plan is intact, it simply
#: cannot proceed until a person answers.
RunStatus = Literal[
    "pending",
    "running",
    "paused",
    "waiting_for_user",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
]

#: What a timeline line is. Kept coarse on purpose — the timeline is for a
#: reader, so it says "read a source", never "called fetch_url with these
#: arguments".
RunStepKind = Literal[
    "run_started",
    "run_finished",
    "task_started",
    "task_completed",
    "task_failed",
    "task_skipped",
    "artifact",
    "source",
    "note",
    "steer",
    "approval",
]

#: What a file in a workspace folder is for. Derived from where it sits, never
#: stored, so moving a file changes its role the way a reader would expect.
ArtifactKind = Literal["document", "note", "data", "upload"]


class WorkspaceTask(BaseModel):
    """One step of the plan, editable by the user and the agent alike."""

    id: str
    content: str
    status: TaskStatus = "pending"
    position: int = 0
    notes: str = ""
    #: Repository-relative path of the artifact this step produced, if any.
    artifact_path: str = ""
    #: Steps that must finish before this one may run. Empty means "the step
    #: before it in the plan", which is what almost every plan means.
    depends_on: list[str] = Field(default_factory=list)
    #: How many times the executor has attempted this step.
    attempts: int = 0
    #: Why the last attempt failed, when one did.
    error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Artifact(BaseModel):
    """A file in the workspace folder, described without loading its content."""

    #: Path relative to the workspace folder, e.g. "findings.md".
    path: str
    #: Path relative to the repository root, which is what tools accept.
    repo_path: str
    title: str
    kind: ArtifactKind = "document"
    suffix: str = ""
    bytes: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: First meaningful line, for a list view that must not read every file.
    preview: str = ""
    #: How many revisions are kept for this file.
    revisions: int = 0
    #: Set on an upload that needed a parser: where its markdown ended up.
    extracted_path: str = ""
    #: Why an upload is unreadable, when it is.
    warning: str = ""
    #: sha256 of the file as it was read. A client sends this back when saving
    #: so an edit written against a version the agent has since replaced is
    #: refused rather than silently overwriting it.
    digest: str = ""


class ArtifactContent(BaseModel):
    """One artifact with its text, for the viewer and the agent."""

    artifact: Artifact
    content: str
    #: False when the file is binary or too large to render.
    readable: bool = True


class Revision(BaseModel):
    """A previous version of an artifact, kept on disk under ``.history``."""

    version: int
    path: str
    author: Literal["user", "agent", "unknown"] = "unknown"
    bytes: int = 0
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchSource(BaseModel):
    """A page the agent read, kept so a claim stays checkable."""

    id: str
    url: str
    title: str = ""
    snippet: str = ""
    #: Repository-relative path of the cached page text.
    cache_path: str = ""
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Workspace(BaseModel):
    """A goal, the folder that holds its work, and the plan to get there."""

    id: str
    name: str
    slug: str
    goal: str = ""
    kind: str = "general"
    #: Repository-relative folder. Everything else is derived from it.
    folder: str
    status: WorkspaceStatus = "active"
    tasks: list[WorkspaceTask] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    uploads: list[Artifact] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    #: The conversation currently attached, so the agent panel can follow it.
    session_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> WorkspaceSummary:
        return WorkspaceSummary(
            id=self.id,
            name=self.name,
            slug=self.slug,
            goal=self.goal,
            kind=self.kind,
            folder=self.folder,
            status=self.status,
            artifact_count=len(self.artifacts),
            upload_count=len(self.uploads),
            task_count=len(self.tasks),
            done_count=sum(item.status == "completed" for item in self.tasks),
            updated_at=self.updated_at,
        )


class WorkspaceSummary(BaseModel):
    """What the workspace list needs, without reading any file's content."""

    id: str
    name: str
    slug: str
    goal: str
    kind: str
    folder: str
    status: WorkspaceStatus
    artifact_count: int
    upload_count: int
    task_count: int
    done_count: int
    updated_at: datetime


class RunStep(BaseModel):
    """One line of a run's timeline, as a reader should see it."""

    id: str
    kind: RunStepKind = "note"
    task_id: str = ""
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkspaceRun(BaseModel):
    """One execution of a workspace's plan.

    Holds only what the plan and the folder cannot: which goal is being worked,
    where the executor is, and why it stopped. Everything it produced is an
    ordinary artifact in the workspace, and every step it worked is an ordinary
    task — so a cancelled run leaves finished work behind rather than unwinding
    it.
    """

    id: str
    workspace_id: str
    goal: str = ""
    status: RunStatus = "pending"
    current_task_id: str = ""
    error: str = ""
    #: The skill guiding this run, when one was selected.
    skill: str = ""
    #: Model profile pinned when the run started, so Resume matches Run.
    profile: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Counts the header needs without loading the plan.
    total_tasks: int = 0
    completed_tasks: int = 0
    #: What the run is waiting for, when it is waiting for something.
    pending_approval: PendingApproval | None = None
    steps: list[RunStep] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def active(self) -> bool:
        """Whether the executor is still attached to this run."""
        return self.status in {"pending", "running", "paused", "waiting_for_approval",
                               "waiting_for_user"}


class PendingApproval(BaseModel):
    """One action the run stopped to ask about."""

    id: str
    action: str
    reason: str = ""
    #: The classification that made this need asking. See
    #: :mod:`daino.workbench.approvals`.
    level: str = "external_action"
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


#: What happened to one artifact inside a change set.
ChangeAction = Literal["created", "updated", "deleted"]

#: Whether a reviewer has decided about it yet.
ChangeStatus = Literal["pending", "accepted", "rejected"]

#: A whole change set rolls its entries' decisions up into one of these.
ChangeSetStatus = Literal["open", "accepted", "rejected", "partial"]


class ChangeEntry(BaseModel):
    """One artifact inside a change set, pinned to the revisions around it."""

    id: str
    #: Workspace-relative path of the artifact that changed.
    path: str
    action: ChangeAction = "updated"
    #: The revision it had before this change. 0 means it did not exist.
    before_version: int = 0
    #: The revision this change left it at. 0 means it was deleted.
    after_version: int = 0
    status: ChangeStatus = "pending"
    summary: str = ""


class ChangeSet(BaseModel):
    """Everything one logical agent operation changed, reviewed together."""

    id: str
    workspace_id: str
    run_id: str = ""
    task_id: str = ""
    summary: str = ""
    status: ChangeSetStatus = "open"
    entries: list[ChangeEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


#: What a link points at. ``design`` and ``code`` are how a workspace records
#: work it started in another tab without owning that work.
LinkTargetKind = Literal["artifact", "design", "code", "upload"]

#: How the source depends on the target, named from the source's point of view.
LinkRelation = Literal[
    "derived_from",
    "generated_from",
    "depends_on",
    "implements",
    "describes",
    "references",
]


class ArtifactLink(BaseModel):
    """One relationship: ``source_path`` was made from ``target_path``."""

    id: str
    source_path: str
    source_kind: LinkTargetKind = "artifact"
    target_path: str
    target_kind: LinkTargetKind = "artifact"
    relation: LinkRelation = "references"
    #: A human name for the target when the path alone is opaque — a design id,
    #: most often.
    title: str = ""
    #: The target's revision when the edge was made.
    target_revision: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StaleArtifact(BaseModel):
    """A document written against something that has since changed."""

    link_id: str
    path: str
    #: What it was written from, and has now fallen behind.
    source_of_truth: str
    relation: LinkRelation
    seen_revision: int
    current_revision: int
    reason: str


class StarterArtifact(BaseModel):
    """A document a template creates empty, so the work has somewhere to go."""

    title: str
    filename: str
    outline: list[str] = Field(default_factory=list)


class WorkspaceTemplate(BaseModel):
    """A work type: what to call it, where to start, and how to behave.

    Deliberately not a :class:`daino.playbooks.Playbook`: those require
    ``allowed_tools``, ``verification_steps`` and ``rollback_steps`` drawn from
    a code vocabulary, and nothing executes them. A template here is three
    honest things — starter tasks, starter documents, and a prompt preamble.
    """

    name: str
    title: str
    purpose: str = ""
    starter_tasks: list[str] = Field(default_factory=list)
    starter_artifacts: list[StarterArtifact] = Field(default_factory=list)
    #: Appended to the workspace agent's system prompt.
    preamble: str = ""


WorkspaceRun.model_rebuild()
