"""Typed contracts for memory, retrieval, instructions, and task continuity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    DECISION = "decision"
    FAILURE = "failure"
    USER = "user"
    EPISODE = "episode"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    SCRATCH = "scratch"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVERSED = "reversed"


class PersistentTaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MemoryMatch(BaseModel):
    id: str
    type: MemoryType
    scope: MemoryScope
    project_id: str
    content: str
    summary: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    source: str = ""
    source_type: str = "agent"
    tags: list[str] = Field(default_factory=list)
    status: MemoryStatus = MemoryStatus.ACTIVE
    score: float = 0.0
    why: list[str] = Field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed: datetime | None = None
    last_verified: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkingMemory(BaseModel):
    """Current-task state. It lives in memory and is checkpointed incrementally."""

    task_id: str
    project_id: str
    mission_id: str | None = None
    session_id: str | None = None
    original_request: str
    interpreted_goal: str = ""
    plan: list[dict[str, Any]] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    current_step: str = ""
    pending_steps: list[str] = Field(default_factory=list)
    status: PersistentTaskStatus = PersistentTaskStatus.PENDING
    repository: str
    branch: str | None = None
    files_inspected: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    commands_executed: list[dict[str, Any]] = Field(default_factory=list)
    important_outputs: list[str] = Field(default_factory=list)
    test_status: dict[str, Any] = Field(default_factory=dict)
    unresolved_questions: list[str] = Field(default_factory=list)
    unresolved_problems: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    last_action: str = ""
    compacted_context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EffectiveInstructions(BaseModel):
    text: str = ""
    sources: list[str] = Field(default_factory=list)
    scopes: dict[str, list[str]] = Field(default_factory=dict)


class CompactedContext(BaseModel):
    current_goal: str
    original_requirements: str
    active_plan: list[dict[str, Any]] = Field(default_factory=list)
    completed_work: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    important_code_locations: list[str] = Field(default_factory=list)
    architectural_decisions: list[str] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    test_results: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    current_hypotheses: list[str] = Field(default_factory=list)
    next_recommended_action: str = ""
    recent_conversation: list[dict[str, str]] = Field(default_factory=list)
