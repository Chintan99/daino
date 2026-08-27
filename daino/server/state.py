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
from daino.application.settings_service import SettingsApplicationService
from daino.design import DesignService
from daino.git import GitClient
from daino.observability import AuditLog
from daino.schemas import QAReport
from daino.services import PreviewManager, TerminalManager
from daino.tools.filesystem import FileTools


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
    execution_map: ExecutionMapApplicationService
    checkpoints: CheckpointApplicationService
    repository: RepositoryApplicationService
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

    @property
    def root(self) -> Path:
        return self.context.root

    @classmethod
    def from_context(cls, context: ProjectContext) -> GuiState:
        missions = MissionApplicationService(context)
        return cls(
            context=context,
            missions=missions,
            design=DesignService(context.root, events=context.events),
            terminals=TerminalManager(context.root),
            preview=PreviewManager(context.root),
            files=FileTools(context.root),
            git=GitClient(context.root),
            qa=QAApplicationService(context, missions),
            execution_map=ExecutionMapApplicationService(context),
            checkpoints=CheckpointApplicationService(context),
            repository=RepositoryApplicationService(context),
            providers=ProviderApplicationService(context),
            settings=SettingsApplicationService(context),
            audit=AuditLog(context.root),
        )

    def shutdown(self) -> None:
        self.terminals.close_all()
        self.preview.stop()
        # Stopping the server mid-turn must not leave sleep inhibited.
        self.missions.attention.shutdown()
