"""Shared, process-wide services for one GUI-served project."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from daino.application.checkpoint_service import CheckpointApplicationService
from daino.application.context import ProjectContext
from daino.application.execution_map_service import ExecutionMapApplicationService
from daino.application.mission_service import MissionApplicationService
from daino.application.provider_service import ProviderApplicationService
from daino.application.qa_service import QAApplicationService
from daino.application.repository_service import RepositoryApplicationService
from daino.application.review_service import ChangeReviewApplicationService
from daino.application.settings_service import SettingsApplicationService
from daino.application.workspace_run_service import WorkspaceRunApplicationService
from daino.design import DesignService
from daino.git import GitClient
from daino.observability import AuditLog
from daino.schemas import ChangeReview, QAReport
from daino.services import PreviewManager, TerminalManager
from daino.tools.filesystem import FileTools
from daino.workbench.links import LinkStore
from daino.workbench.service import WorkbenchService


@dataclass
class GuiState:
    """Everything the GUI routes and WebSockets need for one open project.

    Built once at server start from a ``ProjectContext`` so every request and
    socket shares the same agent runtime, event bus, and session store. The
    engineering services (QA, execution map, checkpoints, repository index,
    audit log) are the very same ones the TUI renders, so the browser IDE and
    the terminal client never disagree about what happened.
    """

    context: ProjectContext
    missions: MissionApplicationService
    design: DesignService
    terminals: TerminalManager
    preview: PreviewManager
    files: FileTools
    git: GitClient
    qa: QAApplicationService
    #: Reviews one change rather than the whole repository.
    review: ChangeReviewApplicationService
    execution_map: ExecutionMapApplicationService
    checkpoints: CheckpointApplicationService
    repository: RepositoryApplicationService
    #: Knowledge-work workspaces: goals, documents, tasks, and their sources.
    workbench: WorkbenchService
    #: Executes a workspace's plan, one task per agent turn.
    runs: WorkspaceRunApplicationService
    #: How a workspace's outputs relate, and which have fallen behind.
    links: LinkStore
    #: Provider/model routing and validated configuration writes — the same
    #: services the TUI's providers and settings screens drive.
    providers: ProviderApplicationService
    settings: SettingsApplicationService
    audit: AuditLog
    #: The agentic turn in flight, if any. Shared rather than per-connection
    #: because a turn outlives the socket that started it — a refreshed tab must
    #: still be able to stop it.
    active_turn: object | None = None
    #: Held for the duration of an agentic turn. One runtime, one working tree:
    #: a second browser tab must wait rather than interleave tool calls with the
    #: first. Declaring it was not enough — every turn now actually takes it.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The in-flight QA run, if any, plus the newest report it has emitted. QA is
    #: long-running, so the GUI kicks it off and polls rather than holding a request open.
    qa_task: object | None = None
    qa_live: QAReport | None = None
    #: The in-flight change review, plus the newest report it has emitted. Same
    #: shape as the QA pair above and for the same reason.
    review_task: object | None = None
    review_live: ChangeReview | None = None

    @property
    def root(self) -> Path:
        return self.context.root

    @classmethod
    def from_context(cls, context: ProjectContext) -> GuiState:
        missions = MissionApplicationService(context)
        workbench = WorkbenchService(context.root, context.database, events=context.events)
        # Built here rather than inside the service so the run executor and the
        # browser's own turns contend for the *same* lock: one working tree and
        # one runtime cannot serve two agents at once.
        turn_lock = asyncio.Lock()
        return cls(
            context=context,
            missions=missions,
            turn_lock=turn_lock,
            design=DesignService(context.root, events=context.events),
            terminals=TerminalManager(context.root),
            preview=PreviewManager(context.root),
            files=FileTools(context.root),
            git=GitClient(context.root),
            qa=QAApplicationService(context, missions),
            review=ChangeReviewApplicationService(context, missions),
            execution_map=ExecutionMapApplicationService(context),
            checkpoints=CheckpointApplicationService(context),
            repository=RepositoryApplicationService(context),
            workbench=workbench,
            runs=WorkspaceRunApplicationService(
                context, missions, workbench, turn_lock=turn_lock
            ),
            links=LinkStore(context.database, workbench),
            providers=ProviderApplicationService(context),
            settings=SettingsApplicationService(context),
            audit=AuditLog(context.root),
        )

    def start_watchers(self) -> None:
        """Attach the long-lived subscriptions this project needs.

        Separate from construction because a subscription outlives a request and
        must be attached exactly once: ``WorkbenchService`` is also built
        per-turn inside a chat, and every one of those must not add its own
        revision recorder.
        """
        self.workbench.watch_file_changes(self.context.events)
        # A run the previous process died holding is not running now, whatever
        # its row says. Recovering it here — once, at startup — turns a lie the
        # GUI would render as live work into an honest offer to resume.
        self.runs.reconcile()

    def shutdown(self) -> None:
        self.terminals.close_all()
        self.preview.stop()
        # Stopping the server mid-turn must not leave sleep inhibited.
        self.missions.attention.shutdown()
