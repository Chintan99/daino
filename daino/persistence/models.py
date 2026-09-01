"""SQLAlchemy persistence model for auditable, resumable missions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from daino.utils.time import utcnow


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    root_path: Mapped[str] = mapped_column(Text, unique=True)


class Provider(Base, TimestampMixin):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(Text)
    api_key_reference: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModelProfile(Base, TimestampMixin):
    __tablename__ = "model_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    provider_name: Mapped[str] = mapped_column(String(255))
    model_identifier: Mapped[str] = mapped_column(String(255))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Mission(Base, TimestampMixin):
    __tablename__ = "missions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    request: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    workspace_path: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    initial_revision: Mapped[str | None] = mapped_column(String(64))
    final_revision: Mapped[str | None] = mapped_column(String(64))
    failure: Mapped[str | None] = mapped_column(Text)
    tasks: Mapped[list[Task]] = relationship(back_populates="mission", cascade="all, delete-orphan")


class ConversationSession(Base, TimestampMixin):
    __tablename__ = "conversation_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    active_model: Mapped[str | None] = mapped_column(String(255))
    context_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    display_mode: Mapped[str] = mapped_column(String(32), default="standard")
    status: Mapped[str] = mapped_column(String(32), default="active")
    #: The workspace this conversation belongs to, when it belongs to one. This
    #: single nullable link is what gives knowledge work continuity across many
    #: sessions; a repository chat simply leaves it null.
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)


class ConversationState(Base, TimestampMixin):
    """Mutable workspace state kept separate so old session tables remain compatible."""

    __tablename__ = "conversation_states"
    session_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_sessions.id"), primary_key=True
    )
    interaction_mode: Mapped[str] = mapped_column(String(32), default="ask")
    todos: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id"), index=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class MissionEventRecord(Base, TimestampMixin):
    __tablename__ = "mission_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(96), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RequirementVersion(Base, TimestampMixin):
    __tablename__ = "requirement_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)


class ArchitectureDecision(Base, TimestampMixin):
    __tablename__ = "architecture_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(Text)
    implementation_rule: Mapped[str] = mapped_column(Text)
    related_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="accepted")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    risk_level: Mapped[str] = mapped_column(String(32))
    specification: Mapped[dict[str, Any]] = mapped_column(JSON)
    assigned_model: Mapped[str | None] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    mission: Mapped[Mission] = relationship(back_populates="tasks")


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    depends_on_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text, default="")


class ModelCall(Base, TimestampMixin):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(255))
    selection_reason: Mapped[str] = mapped_column(Text)
    included_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class ToolCall(Base, TimestampMixin):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    tool: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    success: Mapped[bool] = mapped_column(Boolean)


class VerificationRun(Base, TimestampMixin):
    __tablename__ = "verification_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    passed: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict[str, Any]] = mapped_column(JSON)


class Review(Base, TimestampMixin):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict[str, Any]] = mapped_column(JSON)


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Checkpoint(Base, TimestampMixin):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    revision: Mapped[str | None] = mapped_column(String(64))
    archive_path: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)


class DeploymentTarget(Base, TimestampMixin):
    __tablename__ = "deployment_targets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)


class DeploymentRun(Base, TimestampMixin):
    __tablename__ = "deployment_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_name: Mapped[str] = mapped_column(String(255), index=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"))
    release_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_release: Mapped[str | None] = mapped_column(String(128))


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean)
    approver: Mapped[str] = mapped_column(String(255), default="cli-user")


class PlaybookExecution(Base, TimestampMixin):
    __tablename__ = "playbook_executions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"))
    playbook: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemoryRecord(Base, TimestampMixin):
    __tablename__ = "memory_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(255))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    related_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    human_approval_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    # ``category``/``content``/``human_approval_status`` are retained for
    # backwards compatibility with the original small memory store.  The
    # fields below form the richer, typed memory envelope used by
    # ``MemoryManager``.
    memory_type: Mapped[str] = mapped_column(String(32), default="semantic", index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source_type: Mapped[str] = mapped_column(String(32), default="agent")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    project_revision: Mapped[str | None] = mapped_column(String(64))
    source_digest: Mapped[str | None] = mapped_column(String(128))
    superseded_by: Mapped[str | None] = mapped_column(String(64), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")


class MemoryEmbedding(Base, TimestampMixin):
    """Provider-neutral embedding payload kept separate from memory metadata."""

    __tablename__ = "memory_embeddings"
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255), default="")
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list[float]] = mapped_column(JSON)


class PersistentTaskState(Base, TimestampMixin):
    """Incrementally persisted working state for crash-safe task continuation."""

    __tablename__ = "persistent_task_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_sessions.id"), index=True
    )
    original_request: Mapped[str] = mapped_column(Text)
    interpreted_goal: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    completed_steps: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_step: Mapped[str] = mapped_column(Text, default="")
    pending_steps: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    repository: Mapped[str] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    files_inspected: Mapped[list[str]] = mapped_column(JSON, default=list)
    files_changed: Mapped[list[str]] = mapped_column(JSON, default=list)
    commands_executed: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    important_outputs: Mapped[list[str]] = mapped_column(JSON, default=list)
    test_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    unresolved_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    unresolved_problems: Mapped[list[str]] = mapped_column(JSON, default=list)
    hypotheses: Mapped[list[str]] = mapped_column(JSON, default=list)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_action: Mapped[str] = mapped_column(Text, default="")
    compacted_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemoryEpisode(Base, TimestampMixin):
    """A compact, useful session outcome rather than a raw transcript."""

    __tablename__ = "memory_episodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    goal: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    major_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    discoveries: Mapped[list[str]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
    files_changed: Mapped[list[str]] = mapped_column(JSON, default=list)
    commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    test_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(Text, default="")
    unresolved_work: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class Workspace(Base, TimestampMixin):
    """A named body of knowledge work: a goal, its folder, and its sessions.

    The missing container. ``Project`` is the directory, ``Mission`` is one
    request bound to a git worktree, and ``ConversationSession`` is one chat.
    Nothing named a goal that outlives a single conversation, which is what
    documents, research, and analysis actually need.

    The row is an index, never the source of truth: the artifacts live as real
    files under ``folder`` so they are greppable, indexable, and diffable like
    anything else in the repository.
    """

    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text, default="")
    #: The work-type template this workspace was created from.
    kind: Mapped[str] = mapped_column(String(32), default="general")
    #: Repository-relative folder holding uploads, artifacts, and history.
    folder: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class WorkspaceTask(Base, TimestampMixin):
    """One step of a workspace's plan, editable by both the user and the agent.

    Deliberately not a mission ``Task``: those are terminal on completion, die
    with their mission, and require acceptance criteria plus runnable
    verification commands. And deliberately not a session todo: those have no
    id, no order, and are wiped between turns. This is the durable middle.
    """

    __tablename__ = "workspace_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    #: Shares the vocabulary of ``TodoItem`` so the existing renderers apply,
    #: but no status here is terminal — a workspace task can be reopened.
    status: Mapped[str] = mapped_column(String(32), default="pending")
    position: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    #: The artifact this task produced, repository-relative, when there is one.
    artifact_path: Mapped[str] = mapped_column(Text, default="")


class WorkspaceSource(Base, TimestampMixin):
    """One page the agent read while researching, kept so it can be cited.

    Registered automatically whenever a fetch succeeds in a workspace, because
    a citation the model has to remember to record is a citation that goes
    missing. The fetched text is cached on disk so a claim stays checkable after
    the page changes.
    """

    __tablename__ = "workspace_sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(512), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    digest: Mapped[str] = mapped_column(String(128), default="")
    #: Repository-relative path of the cached page text.
    cache_path: Mapped[str] = mapped_column(Text, default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
