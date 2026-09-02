"""Workspaces: a goal, a folder of real files, a plan, and the sources behind it.

Mirrors :class:`daino.design.DesignService` in shape — granular mutators that
each publish an event so the browser follows the agent live — but differs in one
decision that changes everything downstream: **a workspace's documents are
ordinary files in the repository, not nodes inside one JSON blob.**

That choice is why this module is mostly path arithmetic. The agent needs no new
file tools, because ``read_file`` / ``write`` / ``replace`` / ``grep`` already
work on any path under the repository root. The database row is an index for
listing and querying; the folder is the truth. Delete the row and the work
survives; delete the folder and the row is describing nothing.

Those folders live under ``.daino/workspaces/`` rather than in the working tree.
Knowledge work is the project's, but it is not its source: a documents folder at
the repository root turns up in every diff, every file tree, and every package
listing. Inside the state directory it stays out of the way, and the search
tools are told to look there anyway (:func:`daino.config.paths.in_workspaces`).

Everything that accepts a caller-supplied path resolves it and checks
containment before touching the disk. A workspace is a boundary, not a hint.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from daino.config import paths
from daino.events import (
    EventBus,
    EventSubscription,
    FileChanged,
    GitChanged,
    MissionEvent,
    WorkspaceCreated,
    WorkspaceUpdated,
)
from daino.persistence import Database
from daino.persistence.models import ConversationSession
from daino.persistence.models import Workspace as WorkspaceRow
from daino.persistence.models import WorkspaceSource as SourceRow
from daino.persistence.models import WorkspaceTask as TaskRow
from daino.utils.ids import new_id
from daino.workbench import deliverables, extraction
from daino.workbench.models import (
    Artifact,
    ArtifactContent,
    ArtifactKind,
    ResearchSource,
    Revision,
    TaskStatus,
    Workspace,
    WorkspaceStatus,
    WorkspaceSummary,
    WorkspaceTask,
    WorkspaceTemplate,
)
from daino.workbench.templates import TemplateLoader

#: Default parent for new workspace folders, relative to the repository root:
#: ``.daino/workspaces`` (or ``.vasuki/workspaces`` in a legacy checkout).
#: Resolved per service instance because the state directory depends on which
#: marker the project already carries.

#: Subdirectories the workspace owns. None of them is an artifact.
UPLOADS_DIR = "uploads"
SOURCES_DIR = ".sources"
HISTORY_DIR = ".history"
MANIFEST = "workspace.json"

_RESERVED = frozenset({UPLOADS_DIR, SOURCES_DIR, HISTORY_DIR, MANIFEST})

#: A document larger than this is listed but not loaded into the viewer or the
#: model. Matches the editor's own ceiling so the two never disagree.
MAX_ARTIFACT_BYTES = 2_000_000
#: Enough of a document to recognise it in a list without reading the file.
PREVIEW_CHARS = 160
#: Revisions are a safety net, not an archive.
MAX_REVISIONS = 50


class WorkbenchError(ValueError):
    """Raised for an unknown workspace, a bad path, or an unreadable file."""


class StaleArtifactError(WorkbenchError):
    """Raised when a write is based on a version the file no longer holds.

    Carries the digest the file actually has, so a caller can offer "reload" and
    "keep mine" without a second round trip to find out what it is now.
    """

    def __init__(self, message: str, *, current_digest: str = "") -> None:
        super().__init__(message)
        self.current_digest = current_digest


class WorkbenchService:
    """Create and mutate workspaces, their files, tasks, and sources."""

    def __init__(
        self,
        root: Path,
        database: Database,
        *,
        events: EventBus | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.database = database
        self.events = events
        self.templates = TemplateLoader(self.root)
        #: Repository-relative parent for new workspaces, e.g.
        #: ``.daino/workspaces``. A workspace still records its own folder, so
        #: one created elsewhere — before this default moved, or with an
        #: explicit folder — keeps working exactly where it is.
        self.workspaces_folder = paths.workspaces_dir(self.root).relative_to(self.root).as_posix()

    # ------------------------------------------------------------ workspaces

    def list_workspaces(self, *, include_archived: bool = False) -> list[WorkspaceSummary]:
        with self.database.session() as session:
            query = select(WorkspaceRow).where(
                WorkspaceRow.project_id == self.database.project().id
            )
            if not include_archived:
                query = query.where(WorkspaceRow.status == "active")
            rows = session.scalars(query.order_by(WorkspaceRow.updated_at.desc())).all()
            identifiers = [row.id for row in rows]
        return [self.get(identifier).summary() for identifier in identifiers]

    def create(
        self,
        name: str,
        *,
        goal: str = "",
        kind: str = "general",
        folder: str = "",
    ) -> Workspace:
        """Create a workspace and scaffold its folder from the template."""
        cleaned = name.strip() or "Untitled workspace"
        template = self.templates.get(kind)
        slug = _slug(cleaned)
        relative = folder.strip().strip("/") or f"{self.workspaces_folder}/{slug}"
        relative = self._unique_folder(relative)
        directory = self._within_root(relative)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / UPLOADS_DIR).mkdir(exist_ok=True)

        identifier = new_id("ws")
        with self.database.session() as session:
            session.add(
                WorkspaceRow(
                    id=identifier,
                    project_id=self.database.project().id,
                    name=cleaned,
                    slug=slug,
                    goal=goal.strip(),
                    kind=template.name,
                    folder=relative,
                )
            )
            for position, content in enumerate(template.starter_tasks):
                session.add(
                    TaskRow(
                        id=new_id("wstask"),
                        workspace_id=identifier,
                        content=content,
                        position=position,
                    )
                )
        self._scaffold_artifacts(directory, template, goal=goal)
        self._write_manifest(directory, identifier, cleaned, goal, template.name)
        self._publish(
            WorkspaceCreated(
                workspace_id=identifier,
                name=cleaned,
                kind=template.name,
                folder=relative,
            )
        )
        return self.get(identifier)

    def get(self, workspace_id: str) -> Workspace:
        """Load one workspace: its row, its folder, its tasks, and its sources."""
        with self.database.session() as session:
            row = session.get(WorkspaceRow, workspace_id)
            if row is None:
                raise WorkbenchError(f"Unknown workspace {workspace_id}")
            record = WorkspaceRecord(
                id=row.id,
                name=row.name,
                slug=row.slug,
                goal=row.goal,
                kind=row.kind,
                folder=row.folder,
                status=row.status,
                metadata=dict(row.metadata_json or {}),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            tasks = [
                _task(item)
                for item in session.scalars(
                    select(TaskRow)
                    .where(TaskRow.workspace_id == workspace_id)
                    .order_by(TaskRow.position, TaskRow.created_at)
                ).all()
            ]
            sources = [
                _source(item)
                for item in session.scalars(
                    select(SourceRow)
                    .where(SourceRow.workspace_id == workspace_id)
                    .order_by(SourceRow.retrieved_at.desc())
                ).all()
            ]
            attached = session.scalar(
                select(ConversationSession)
                .where(ConversationSession.workspace_id == workspace_id)
                .order_by(ConversationSession.updated_at.desc())
            )
            session_id = attached.id if attached is not None else ""

        directory = self._within_root(record.folder)
        return Workspace(
            id=record.id,
            name=record.name,
            slug=record.slug,
            goal=record.goal,
            kind=record.kind,
            folder=record.folder,
            status=_workspace_status(record.status),
            metadata=record.metadata,
            created_at=record.created_at,
            updated_at=record.updated_at,
            tasks=tasks,
            sources=sources,
            session_id=session_id,
            artifacts=self._scan_artifacts(directory, record.folder),
            uploads=self._scan_uploads(directory, record.folder),
        )

    def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        goal: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> Workspace:
        with self.database.session() as session:
            row = session.get(WorkspaceRow, workspace_id)
            if row is None:
                raise WorkbenchError(f"Unknown workspace {workspace_id}")
            if name is not None and name.strip():
                row.name = name.strip()
            if goal is not None:
                row.goal = goal.strip()
            if kind is not None:
                row.kind = self.templates.get(kind).name
            if status is not None:
                if status not in {"active", "archived"}:
                    raise WorkbenchError(f"Unknown workspace status {status}")
                row.status = status
            directory = self._within_root(row.folder)
            manifest = (row.id, row.name, row.goal, row.kind)
        self._write_manifest(directory, *manifest)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="workspace"))
        return self.get(workspace_id)

    def delete(self, workspace_id: str, *, remove_files: bool = False) -> None:
        """Delete the row, and optionally the folder it describes.

        The two are separable on purpose. Removing a workspace from the list is
        cheap and reversible; deleting a folder of written work is neither, so
        it takes a second, explicit decision.
        """
        with self.database.session() as session:
            row = session.get(WorkspaceRow, workspace_id)
            if row is None:
                raise WorkbenchError(f"Unknown workspace {workspace_id}")
            directory = self._within_root(row.folder)
            for task in session.scalars(
                select(TaskRow).where(TaskRow.workspace_id == workspace_id)
            ).all():
                session.delete(task)
            for source in session.scalars(
                select(SourceRow).where(SourceRow.workspace_id == workspace_id)
            ).all():
                session.delete(source)
            for attached in session.scalars(
                select(ConversationSession).where(ConversationSession.workspace_id == workspace_id)
            ).all():
                # Keep the conversation; it just stops belonging to anything.
                attached.workspace_id = None
            session.delete(row)
        if remove_files and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            _prune_empty_parent(directory.parent, self.root)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="deleted"))

    def attach_session(self, workspace_id: str, session_id: str) -> None:
        """Point a conversation at this workspace, so its history accumulates."""
        with self.database.session() as session:
            row = session.get(ConversationSession, session_id)
            if row is None:
                raise WorkbenchError(f"Unknown session {session_id}")
            if session.get(WorkspaceRow, workspace_id) is None:
                raise WorkbenchError(f"Unknown workspace {workspace_id}")
            row.workspace_id = workspace_id

    def workspace_for_session(self, session_id: str) -> str | None:
        """Which workspace a conversation belongs to, if any.

        The inverse of :meth:`attach_session`, and the lookup that lets a
        message typed into the shared agent panel be recognised as direction for
        a running plan rather than as a new turn.
        """
        with self.database.session() as session:
            row = session.get(ConversationSession, session_id)
            return row.workspace_id if row is not None else None

    def watch_file_changes(self, events: EventBus) -> EventSubscription:
        """Record a revision whenever a workspace document changes on disk.

        Driven by events rather than by the write path, because the edits that
        most need a way back are the ones that did not come through this
        service. Two events, because the product emits two: the agent's turn
        reports ``FileChanged`` per edit, while a manual save from the CODE tab
        reports ``GitChanged``. Watching only one would have captured only one
        author, and the pair is exactly what a reader needs to compare.
        """

        def observe(event: MissionEvent) -> None:
            for path, author in _changed_paths(event):
                workspace_id = self.workspace_for_path(path)
                if workspace_id is None:
                    continue
                relative = _relative_to(path, self.get(workspace_id).folder)
                if relative is None or _is_reserved(relative):
                    continue
                self.record_revision(workspace_id, relative, author=author)

        return events.subscribe(observe)

    def workspace_for_path(self, relative: str) -> str | None:
        """Which workspace, if any, owns a repository-relative path.

        Used by the history subscriber: a file change only becomes a revision
        when it lands inside a workspace folder.
        """
        candidate = Path(relative.strip().lstrip("/"))
        with self.database.session() as session:
            rows = session.scalars(
                select(WorkspaceRow).where(WorkspaceRow.project_id == self.database.project().id)
            ).all()
            folders = [(row.id, row.folder) for row in rows]
        for identifier, folder in folders:
            if candidate.is_relative_to(Path(folder)):
                return identifier
        return None

    # ------------------------------------------------------------- artifacts

    def artifact(self, workspace_id: str, relative: str) -> ArtifactContent:
        """One artifact with its text, or a description of why it has none."""
        workspace = self.get(workspace_id)
        path = self._within_workspace(workspace, relative)
        if not path.is_file():
            raise WorkbenchError(f"{relative} does not exist in this workspace")
        described = self._describe(path, workspace)
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            return ArtifactContent(
                artifact=described,
                content="",
                readable=False,
            )
        # Digested on the single-document read rather than in the folder scan:
        # this is where an editable draft starts, and hashing every file to
        # render a list would make opening a workspace pay for it.
        described = described.model_copy(update={"digest": extraction.file_digest(path)})
        try:
            return ArtifactContent(
                artifact=described,
                content=path.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeDecodeError):
            return ArtifactContent(artifact=described, content="", readable=False)

    def write_artifact(
        self,
        workspace_id: str,
        relative: str,
        content: str,
        *,
        author: str = "user",
        base_digest: str = "",
    ) -> Artifact:
        """Create or replace an artifact, keeping the previous text as a revision.

        ``base_digest`` is optimistic concurrency: the digest the writer's draft
        was based on. When it does not match what is on disk, somebody else — an
        agent finishing a step, another window — has rewritten the document
        since, and the write is refused. History makes such a loss recoverable;
        refusing means it never happens.
        """
        workspace = self.get(workspace_id)
        path = self._within_workspace(workspace, relative)
        if path.name in _RESERVED or _reserved_parent(path, self._within_root(workspace.folder)):
            raise WorkbenchError(f"{relative} is reserved by the workspace")
        if base_digest:
            current = extraction.file_digest(path) if path.is_file() else ""
            if current != base_digest:
                raise StaleArtifactError(
                    f"{relative} has changed since you opened it — most likely the "
                    "agent rewrote it. Reload it, or keep your version explicitly.",
                    current_digest=current,
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        # Recorded after the write, so a revision is a version that existed and
        # its author is whoever wrote it — not whoever happened to overwrite it.
        self.record_revision(workspace_id, relative, author=author)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="artifact", path=relative))
        return self._describe(path, workspace)

    def save_deliverable(
        self, workspace_id: str, source_relative: str, fmt: str, *, title: str = ""
    ) -> Artifact:
        """Render a workspace document into a finished file beside it.

        The markdown stays authoritative; this is a rendering of it, which is
        why it is regenerated rather than edited. Written through the same
        containment check and revision recording as any other artifact, so a
        regenerated deck is as recoverable as a rewritten document.
        """
        source = self.artifact(workspace_id, source_relative)
        if not source.readable:
            raise WorkbenchError(
                f"{source_relative} cannot be read as text, so it cannot be rendered."
            )
        try:
            payload = deliverables.render(
                source.content, fmt, title=title or source.artifact.title
            )
        except deliverables.DeliverableError as exc:
            raise WorkbenchError(str(exc)) from exc
        relative = deliverables.deliverable_path(source_relative, fmt)
        workspace = self.get(workspace_id)
        path = self._within_workspace(workspace, relative)
        if path.name in _RESERVED or _reserved_parent(path, self._within_root(workspace.folder)):
            raise WorkbenchError(f"{relative} is reserved by the workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        self.record_revision(workspace_id, relative, author="agent")
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="artifact", path=relative))
        return self._describe(path, workspace)

    def delete_artifact(self, workspace_id: str, relative: str) -> None:
        workspace = self.get(workspace_id)
        path = self._within_workspace(workspace, relative)
        if not path.is_file():
            raise WorkbenchError(f"{relative} does not exist in this workspace")
        self.record_revision(workspace_id, relative, author="user")
        path.unlink()
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="artifact", path=relative))

    # -------------------------------------------------------------- history

    def record_revision(self, workspace_id: str, relative: str, *, author: str) -> None:
        """Record the artifact's current text as its next version.

        Called after every write and from the ``FileChanged`` subscriber, so an
        agent rewriting a document the user had edited is always recoverable.
        Content identical to the newest revision is ignored: several tools may
        touch one file in a single turn, and that is one version, not four.
        """
        workspace = self.get(workspace_id)
        path = self._within_workspace(workspace, relative)
        if not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return
        try:
            content = path.read_bytes()
        except OSError:
            return
        directory = self._within_root(workspace.folder)
        index = _read_index(directory)
        entries = index.setdefault(relative, [])
        if entries and entries[-1].get("digest") == extraction.file_digest(path):
            # Nothing changed since the last snapshot; do not fill history with
            # duplicates when several tools touch the same file in one turn.
            return
        version = int(entries[-1]["version"]) + 1 if entries else 1
        blob = directory / HISTORY_DIR / _history_stem(relative) / f"{version}{path.suffix}"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
        entries.append(
            {
                "version": version,
                "author": author,
                "saved_at": datetime.now(UTC).isoformat(),
                "bytes": len(content),
                "digest": extraction.file_digest(path),
            }
        )
        if len(entries) > MAX_REVISIONS:
            # Trim the oldest, but never a version something still points at.
            # Change sets record "this step moved the file from v3 to v4", and
            # rejecting or diffing one reads those blobs back — so dropping a
            # pinned revision turns an undo button into an error message.
            cutoff = len(entries) - MAX_REVISIONS
            kept: list[dict[str, Any]] = []
            for position, entry in enumerate(entries):
                if position >= cutoff or entry.get("pinned"):
                    kept.append(entry)
                    continue
                _unlink(directory / HISTORY_DIR / _history_stem(relative) / _blob_name(entry, path))
            index[relative] = kept
        _write_index(directory, index)

    def pin_revisions(self, workspace_id: str, relative: str, versions: Iterable[int]) -> None:
        """Exempt these versions of an artifact from history trimming.

        Called when a change set starts referring to them. The retention cap is
        a safety net against unbounded history, not a licence to delete a blob
        another feature has promised to show: an old change set whose "before"
        blob was pruned can be neither diffed nor rejected.
        """
        wanted = {int(version) for version in versions if int(version) > 0}
        if not wanted:
            return
        workspace = self.get(workspace_id)
        directory = self._within_root(workspace.folder)
        index = _read_index(directory)
        entries = index.get(relative)
        if not entries:
            return
        changed = False
        for entry in entries:
            if int(entry.get("version", 0)) in wanted and not entry.get("pinned"):
                entry["pinned"] = True
                changed = True
        if changed:
            _write_index(directory, index)

    def revisions(self, workspace_id: str, relative: str) -> list[Revision]:
        workspace = self.get(workspace_id)
        directory = self._within_root(workspace.folder)
        entries = _read_index(directory).get(relative, [])
        suffix = Path(relative).suffix
        return [
            Revision(
                version=int(entry["version"]),
                path=f"{HISTORY_DIR}/{_history_stem(relative)}/{entry['version']}{suffix}",
                author=entry.get("author", "unknown"),
                bytes=int(entry.get("bytes", 0)),
                saved_at=_parse_time(entry.get("saved_at")),
            )
            for entry in reversed(entries)
        ]

    def revision_content(self, workspace_id: str, relative: str, version: int) -> str:
        workspace = self.get(workspace_id)
        directory = self._within_root(workspace.folder)
        blob = (
            directory / HISTORY_DIR / _history_stem(relative) / f"{version}{Path(relative).suffix}"
        )
        if not blob.is_file():
            raise WorkbenchError(f"{relative} has no revision {version}")
        try:
            return blob.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkbenchError(f"Revision {version} of {relative} is unreadable") from exc

    def restore_revision(self, workspace_id: str, relative: str, version: int) -> Artifact:
        """Put an old revision back, keeping the current text as a new revision."""
        content = self.revision_content(workspace_id, relative, version)
        return self.write_artifact(workspace_id, relative, content, author="user")

    # ---------------------------------------------------------------- tasks

    def set_tasks(self, workspace_id: str, contents: list[str]) -> list[WorkspaceTask]:
        """Replace the whole plan, preserving the status of steps that survive.

        Matching on text is imperfect, but the alternative — resetting every
        status whenever the agent re-emits the plan — loses real progress, which
        is worse than occasionally carrying a status onto a reworded step.
        """
        with self.database.session() as session:
            self._require(session, workspace_id)
            existing = {
                item.content: item
                for item in session.scalars(
                    select(TaskRow).where(TaskRow.workspace_id == workspace_id)
                ).all()
            }
            keep: set[str] = set()
            for position, content in enumerate(contents):
                cleaned = content.strip()
                if not cleaned:
                    continue
                current = existing.get(cleaned)
                if current is None:
                    session.add(
                        TaskRow(
                            id=new_id("wstask"),
                            workspace_id=workspace_id,
                            content=cleaned,
                            position=position,
                        )
                    )
                else:
                    current.position = position
                    keep.add(cleaned)
            for content, row in existing.items():
                if content not in keep:
                    session.delete(row)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="tasks"))
        return self.get(workspace_id).tasks

    def add_task(self, workspace_id: str, content: str) -> WorkspaceTask:
        cleaned = content.strip()
        if not cleaned:
            raise WorkbenchError("A task needs some text")
        with self.database.session() as session:
            self._require(session, workspace_id)
            last = session.scalars(
                select(TaskRow)
                .where(TaskRow.workspace_id == workspace_id)
                .order_by(TaskRow.position.desc())
            ).first()
            row = TaskRow(
                id=new_id("wstask"),
                workspace_id=workspace_id,
                content=cleaned,
                position=(last.position + 1) if last is not None else 0,
            )
            session.add(row)
            identifier = row.id
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="tasks"))
        return next(item for item in self.get(workspace_id).tasks if item.id == identifier)

    def update_task(
        self,
        workspace_id: str,
        task_id: str,
        *,
        content: str | None = None,
        status: TaskStatus | None = None,
        notes: str | None = None,
        artifact_path: str | None = None,
        depends_on: list[str] | None = None,
        error: str | None = None,
    ) -> WorkspaceTask:
        with self.database.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None or row.workspace_id != workspace_id:
                raise WorkbenchError(f"Unknown task {task_id}")
            if content is not None and content.strip():
                row.content = content.strip()
            if status is not None:
                row.status = status
            if notes is not None:
                row.notes = notes
            if artifact_path is not None:
                row.artifact_path = artifact_path
            if depends_on is not None:
                row.depends_on = [item for item in depends_on if item and item != task_id]
            if error is not None:
                row.error = error
            if status == "in_progress":
                # Counted here rather than by the executor so a hand-run step
                # and an executed one are tallied the same way.
                row.attempts = (row.attempts or 0) + 1
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="tasks"))
        return next(item for item in self.get(workspace_id).tasks if item.id == task_id)

    def reorder_tasks(self, workspace_id: str, task_ids: list[str]) -> list[WorkspaceTask]:
        with self.database.session() as session:
            rows = {
                item.id: item
                for item in session.scalars(
                    select(TaskRow).where(TaskRow.workspace_id == workspace_id)
                ).all()
            }
            unknown = set(task_ids) - rows.keys()
            if unknown:
                raise WorkbenchError(f"Unknown task(s): {', '.join(sorted(unknown))}")
            for position, task_id in enumerate(task_ids):
                rows[task_id].position = position
            # Anything the caller omitted keeps its relative order behind them.
            for offset, row in enumerate(
                sorted(
                    (item for key, item in rows.items() if key not in set(task_ids)),
                    key=lambda item: item.position,
                )
            ):
                row.position = len(task_ids) + offset
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="tasks"))
        return self.get(workspace_id).tasks

    def delete_task(self, workspace_id: str, task_id: str) -> None:
        with self.database.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None or row.workspace_id != workspace_id:
                raise WorkbenchError(f"Unknown task {task_id}")
            session.delete(row)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="tasks"))

    # -------------------------------------------------------------- sources

    def record_source(
        self,
        workspace_id: str,
        *,
        url: str,
        title: str = "",
        snippet: str = "",
        text: str = "",
    ) -> ResearchSource:
        """Register a page the agent read, caching its text so claims stay checkable.

        Re-reading a URL updates the existing record rather than adding a second
        one: the Sources panel is a bibliography, not a request log.
        """
        digest = extraction.file_digest_of_text(text) if text else ""
        workspace = self.get(workspace_id)
        cache_relative = ""
        if text:
            directory = self._within_root(workspace.folder) / SOURCES_DIR
            directory.mkdir(parents=True, exist_ok=True)
            cached = directory / f"{digest[:16]}.md"
            cached.write_text(
                f"<!-- daino-source\nurl: {url}\ntitle: {title}\n-->\n\n{text}\n",
                encoding="utf-8",
            )
            cache_relative = f"{workspace.folder}/{SOURCES_DIR}/{cached.name}"

        with self.database.session() as session:
            self._require(session, workspace_id)
            row = session.scalar(
                select(SourceRow).where(
                    SourceRow.workspace_id == workspace_id, SourceRow.url == url
                )
            )
            if row is None:
                # Column defaults only apply at INSERT, and this row is read
                # back before the flush, so every field is set explicitly.
                row = SourceRow(
                    id=new_id("wssrc"),
                    workspace_id=workspace_id,
                    url=url,
                    title="",
                    snippet="",
                    digest="",
                    cache_path="",
                )
                session.add(row)
            row.title = title or row.title
            row.snippet = snippet or row.snippet
            row.digest = digest or row.digest
            row.cache_path = cache_relative or row.cache_path
            row.retrieved_at = datetime.now(UTC)
            recorded = _source(row)
        self._publish(WorkspaceUpdated(workspace_id=workspace_id, change="source"))
        return recorded

    # -------------------------------------------------------------- uploads

    def save_upload(self, workspace_id: str, filename: str, payload: bytes) -> Artifact:
        """Store an upload and extract its text, so the agent can read it.

        Name handling mirrors ``POST /api/files/attach``: sanitise, never
        overwrite. Extraction failure is recorded on the artifact rather than
        raised — the file is safely stored either way, and "stored but I cannot
        read it" is a useful state to be able to see.
        """
        workspace = self.get(workspace_id)
        directory = self._within_root(workspace.folder) / UPLOADS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        target = _unique_file(directory, _safe_name(filename))
        target.write_bytes(payload)

        warning = ""
        extracted = ""
        try:
            result, cache = extraction.extract_to_cache(target)
            extracted = cache.relative_to(self.root).as_posix()
            warning = "; ".join(result.warnings)
        except extraction.ExtractionError as exc:
            warning = str(exc)
        self._publish(
            WorkspaceUpdated(
                workspace_id=workspace_id,
                change="upload",
                path=target.relative_to(self.root).as_posix(),
            )
        )
        described = self._describe(target, workspace, kind="upload")
        return described.model_copy(update={"extracted_path": extracted, "warning": warning})

    # ------------------------------------------------------------- internals

    def _require(self, session: Session, workspace_id: str) -> WorkspaceRow:
        row = session.get(WorkspaceRow, workspace_id)
        if row is None:
            raise WorkbenchError(f"Unknown workspace {workspace_id}")
        return row

    def _within_root(self, relative: str) -> Path:
        """Resolve a repository-relative path, refusing anything that escapes."""
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise WorkbenchError(f"Path escapes the project: {relative}")
        return target

    def _within_workspace(self, workspace: Workspace, relative: str) -> Path:
        base = self._within_root(workspace.folder)
        cleaned = str(relative).strip().lstrip("/")
        if not cleaned:
            raise WorkbenchError("A path is required")
        target = (base / cleaned).resolve()
        if not target.is_relative_to(base):
            raise WorkbenchError(f"Path escapes the workspace: {relative}")
        return target

    def _unique_folder(self, relative: str) -> str:
        """Never adopt a directory that already holds someone else's files."""
        candidate = relative
        for attempt in range(2, 1000):
            path = self._within_root(candidate)
            if not path.exists() or not any(path.iterdir()):
                return candidate
            candidate = f"{relative}-{attempt}"
        raise WorkbenchError(f"Could not find a free folder near {relative}")

    def _scaffold_artifacts(
        self, directory: Path, template: WorkspaceTemplate, *, goal: str
    ) -> None:
        for starter in template.starter_artifacts:
            path = directory / _safe_name(starter.filename)
            if path.exists():
                continue
            lines = [f"# {starter.title}", ""]
            if goal.strip():
                lines.extend([f"> {goal.strip()}", ""])
            for heading in starter.outline:
                lines.extend([f"## {heading}", "", "_Not written yet._", ""])
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_manifest(
        self, directory: Path, workspace_id: str, name: str, goal: str, kind: str
    ) -> None:
        """Leave the folder self-describing, so it survives without the database."""
        payload = {
            "id": workspace_id,
            "name": name,
            "goal": goal,
            "kind": kind,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / MANIFEST).write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            # A read-only checkout can still list and read a workspace.
            return

    def _scan_artifacts(self, directory: Path, folder: str) -> list[Artifact]:
        if not directory.is_dir():
            return []
        found = [self._describe_path(path, directory, folder) for path in _iter_files(directory)]
        return sorted(found, key=lambda item: item.path)

    def _scan_uploads(self, directory: Path, folder: str) -> list[Artifact]:
        uploads = directory / UPLOADS_DIR
        if not uploads.is_dir():
            return []
        found: list[Artifact] = []
        for path in sorted(uploads.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            described = self._describe_path(path, directory, folder, kind="upload")
            cache = Path(extraction.extracted_path(path))
            found.append(
                described.model_copy(
                    update={
                        "extracted_path": (
                            cache.relative_to(self.root).as_posix() if cache.is_file() else ""
                        ),
                        "warning": (
                            ""
                            if cache.is_file() or not extraction.needs_extraction(path)
                            else extraction.missing_extra_message(path.suffix)
                        ),
                    }
                )
            )
        return found

    def _describe(
        self, path: Path, workspace: Workspace, *, kind: ArtifactKind | None = None
    ) -> Artifact:
        directory = self._within_root(workspace.folder)
        return self._describe_path(path, directory, workspace.folder, kind=kind)

    def _describe_path(
        self,
        path: Path,
        directory: Path,
        folder: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> Artifact:
        relative = path.relative_to(directory).as_posix()
        stat = path.stat()
        index = _read_index(directory)
        return Artifact(
            path=relative,
            repo_path=f"{folder}/{relative}",
            title=_title(path),
            kind=kind or _kind(path),
            suffix=path.suffix.casefold(),
            bytes=stat.st_size,
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            preview=_preview(path),
            revisions=len(index.get(relative, [])),
        )

    def _publish(self, event: Any) -> None:
        if self.events is not None:
            self.events.publish(event)


# ---------------------------------------------------------------- utilities


def _changed_paths(event: MissionEvent) -> list[tuple[str, str]]:
    """Paths this event says changed, each with who changed them.

    An event carrying a mission is an agent turn; anything else is the person at
    the keyboard, whether they saved from the editor or moved a file.
    """
    if isinstance(event, FileChanged) and event.path:
        return [(event.path, "agent" if event.mission_id else "user")]
    if isinstance(event, GitChanged):
        author = "agent" if event.mission_id else "user"
        return [(path, author) for path in event.paths if path]
    return []


def _relative_to(repo_path: str, folder: str) -> str | None:
    """A workspace-relative path, or None when the file is not in the folder."""
    try:
        return Path(repo_path.strip().lstrip("/")).relative_to(Path(folder)).as_posix()
    except ValueError:
        return None


def _is_reserved(relative: str) -> bool:
    parts = Path(relative).parts
    return bool(parts) and (parts[0] in _RESERVED or parts[0].startswith("."))


def _prune_empty_parent(directory: Path, root: Path) -> None:
    """Remove ``.daino/workspaces/`` once it holds nothing, rather than leaving
    cruft.

    Deleting the last workspace should leave the project as it was found.
    Anything the user put there keeps the directory alive. Only the immediate
    parent is pruned, so the state directory itself is never touched.
    """
    try:
        if directory == root or not directory.is_dir() or any(directory.iterdir()):
            return
        directory.rmdir()
    except OSError:
        return


def _iter_files(directory: Path) -> Iterator[Path]:
    """Every artifact in a workspace: files the workspace does not own itself."""
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        parts = path.relative_to(directory).parts
        if parts[0] in _RESERVED or any(part.startswith(".") for part in parts):
            continue
        yield path


def _reserved_parent(path: Path, directory: Path) -> bool:
    parts = path.relative_to(directory).parts
    return bool(parts) and parts[0] in _RESERVED


def _kind(path: Path) -> ArtifactKind:
    suffix = path.suffix.casefold()
    # Rendered deliverables count as documents: a .docx of the proposal is the
    # proposal, and filing it as "note" would bury it in the list.
    if suffix in {".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".docx", ".pdf", ".pptx"}:
        return "document"
    if suffix in {".csv", ".tsv", ".json", ".yaml", ".yml", ".xlsx"}:
        return "data"
    return "note"


def _title(path: Path) -> str:
    """A document's own first heading beats its filename."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return line[2:].strip()[:120]
    except (OSError, UnicodeDecodeError):
        pass
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def _preview(path: Path) -> str:
    """Enough to recognise a document without loading it."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            head = handle.read(PREVIEW_CHARS * 8)
    except (OSError, UnicodeDecodeError):
        return ""
    body = [
        line.strip()
        for line in head.splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("<!--")
    ]
    return " ".join(body)[:PREVIEW_CHARS]


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:60] or "workspace"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(value).name).strip(".-")
    return cleaned or "file"


def _unique_file(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for attempt in range(1, 1000):
        candidate = directory / f"{stem}-{attempt}{suffix}"
        if not candidate.exists():
            return candidate
    raise WorkbenchError(f"Could not find a free name for {name}")


def _history_stem(relative: str) -> str:
    """A flat, collision-free directory name for one artifact's revisions."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", relative) or "artifact"


def _blob_name(entry: dict[str, Any], path: Path) -> str:
    return f"{entry['version']}{path.suffix}"


def _read_index(directory: Path) -> dict[str, list[dict[str, Any]]]:
    path = directory / HISTORY_DIR / "index.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_index(directory: Path, index: dict[str, list[dict[str, Any]]]) -> None:
    path = directory / HISTORY_DIR / "index.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """The workspace row, detached from its session so the folder can be read."""

    id: str
    name: str
    slug: str
    goal: str
    kind: str
    folder: str
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def _workspace_status(value: str) -> WorkspaceStatus:
    """Narrow a stored status, defaulting rather than raising on a stale value."""
    return "archived" if value == "archived" else "active"


def _task_status(value: str) -> TaskStatus:
    """Narrow a stored status, defaulting rather than raising on a stale value."""
    return value if value in {"pending", "in_progress", "completed", "failed"} else "pending"  # type: ignore[return-value]


def _task(row: TaskRow) -> WorkspaceTask:
    return WorkspaceTask(
        id=row.id,
        content=row.content,
        status=_task_status(row.status),
        position=row.position,
        notes=row.notes,
        artifact_path=row.artifact_path,
        depends_on=list(row.depends_on or []),
        attempts=row.attempts or 0,
        error=row.error or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _source(row: SourceRow) -> ResearchSource:
    return ResearchSource(
        id=row.id,
        url=row.url,
        title=row.title,
        snippet=row.snippet,
        cache_path=row.cache_path,
        retrieved_at=row.retrieved_at,
    )
