"""Project discovery and dependency assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from vasuki.config import (
    config_path,
    default_settings,
    find_project_root,
    load_settings,
    save_settings,
)
from vasuki.config.models import Settings
from vasuki.events import EventBus, MissionEvent, ModelReasoningChunk, ModelStreamChunk
from vasuki.git import GitClient
from vasuki.memory import MemoryManager
from vasuki.observability import AuditLog
from vasuki.persistence import Database
from vasuki.persistence.models import MissionEventRecord
from vasuki.repository import RepositoryIndexer
from vasuki.runtimes.detect import preferred_runtime
from vasuki.utils.ids import new_id
from vasuki.workspace import WorkspaceManager


@dataclass(slots=True)
class ProjectContext:
    root: Path
    settings: Settings
    database: Database
    events: EventBus
    memory: MemoryManager | None = None

    def close(self) -> None:
        if self.memory is not None:
            self.memory.close()
        self.database.engine.dispose()


class InitializationResult(TypedDict):
    root: str
    files: int
    languages: dict[str, int]
    frameworks: list[str]
    runtimes: dict[str, bool]


def _append_ignore_entries(root: Path) -> None:
    ignore_path = root / ".gitignore"
    existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    entries = [
        ".vasuki/",
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        "node_modules/",
        ".env",
        "*.pem",
        "*.key",
    ]
    missing = [entry for entry in entries if entry not in existing.splitlines()]
    if missing:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        ignore_path.write_text(existing + suffix + "\n".join(missing) + "\n", encoding="utf-8")


def _ensure_git_baseline(root: Path) -> None:
    """Give a greenfield project a revision that checkpoints can reference."""
    git = GitClient(root)
    top_level = git.run("rev-parse", "--show-toplevel", check=False)
    owns_repository = top_level.succeeded and Path(top_level.stdout.strip()).resolve() == root
    if not owns_repository:
        # ``git rev-parse`` also succeeds in a fresh directory nested beneath a
        # parent repository. Initializing here is intentional: Vasuki state,
        # history, checkpoints, and edits must remain scoped to the directory
        # selected during onboarding.
        _append_ignore_entries(root)
        git.run("init", "-b", "main")
    if not git.run("rev-parse", "--verify", "HEAD", check=False).succeeded:
        git.commit("Initialize project")


def initialize_project(root: Path, *, force: bool = False) -> InitializationResult:
    """Initialize a project using the same operations as ``vasuki init``."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = config_path(root)
    if target.exists() and not force:
        raise FileExistsError(f"Already initialized: {target}")
    settings = default_settings(root)
    # Probe once, at initialization, so the project records a runtime this
    # machine can actually use instead of failing every command later.
    settings.runtime.default = preferred_runtime()  # type: ignore[assignment]
    save_settings(settings, root)
    _append_ignore_entries(root)
    _ensure_git_baseline(root)
    database = Database(settings, root)
    database.initialize()
    try:
        index = RepositoryIndexer(root).build()
        runtimes = WorkspaceManager(root).detect_runtimes()
        return {
            "root": str(root),
            "files": len(index.files),
            "languages": index.languages,
            "frameworks": index.frameworks,
            "runtimes": runtimes,
        }
    finally:
        database.engine.dispose()


def open_project(path: Path | None = None) -> ProjectContext:
    root = find_project_root(path)
    settings = load_settings(root)
    if config_path(root).exists():
        # Repair projects initialized by versions that accidentally treated a
        # parent Git checkout as this folder's repository. Without a nested
        # baseline, checkpoints and edits can still escape the selected root.
        _ensure_git_baseline(root)
    database = Database(settings, root)
    database.initialize()
    events = EventBus()
    project_id = database.project().id
    audit = AuditLog(root)

    def persist(event: MissionEvent) -> None:
        # Token-level chunks arrive hundreds of times per call. Writing a row and
        # an audit line for each one would dominate streaming latency. Completed
        # answers are persisted as conversation messages; provider reasoning is
        # intentionally ephemeral and is never written to the project ledger.
        if isinstance(event, (ModelStreamChunk, ModelReasoningChunk)):
            return
        payload = event.payload()
        with database.session() as session:
            session.add(
                MissionEventRecord(
                    id=new_id("event"),
                    project_id=project_id,
                    mission_id=event.mission_id,
                    kind=event.kind,
                    payload=payload,
                )
            )
        audit.emit(
            f"event.{event.kind}",
            mission_id=event.mission_id,
            payload=payload,
        )

    events.subscribe(persist)
    memory = MemoryManager(database, root, settings)
    return ProjectContext(root, settings, database, events, memory)


def adopt_project(root: Path) -> ProjectContext:
    """Set a project up from global configuration and open it, asking nothing.

    Used when a model is already configured globally: everything onboarding
    would have collected is known, so the only work left is the mechanical part —
    write the project file, create its database, index it — and open.
    """
    initialize_project(root)
    return open_project(root)
