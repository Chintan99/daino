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
