"""Shared, process-wide services for one GUI-served project."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

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
from daino.debugger import DebugManager
from daino.design import DesignService
from daino.exceptions import TurnBusy
from daino.git import GitClient
from daino.observability import AuditLog
from daino.repository.lsp import PooledLSPAdapter
from daino.schemas import ChangeReview, QAReport
from daino.services import PreviewManager, TerminalManager
from daino.testing import TestService
from daino.tools.filesystem import FileTools
from daino.workbench.links import LinkStore
from daino.workbench.service import WorkbenchService

T = TypeVar("T")


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
    #: Language-server intelligence. One pool of server processes for the whole
    #: project, shared by every socket and request: starting pyright per
    #: keystroke would be slower than having no diagnostics at all.
    lsp: PooledLSPAdapter
    #: Test discovery and execution. One run at a time per project, because
    #: tests share a working tree and two concurrent runs describe nothing.
    tests: TestService
    #: The debug session, if one is running, and the breakpoints that outlive
    #: it. Server-side so a reloaded tab shows the frame it was stopped at.
    debugger: DebugManager
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
    #: Set the moment a turn is claimed, cleared when it finishes. The lock
    #: alone cannot answer "is a turn running?": a turn is claimed
    #: synchronously and only acquires the lock once its coroutine first runs,
    #: and in that gap a second caller reads ``locked()`` as False and starts
    #: its own. Two agents against one working tree is exactly what the lock
    #: exists to prevent, so the claim closes the gap.
    turn_claimed: bool = False
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

    # ------------------------------------------------------------------ turns

    @property
    def turn_busy(self) -> bool:
        """Whether an agentic turn is running or about to be."""
        return self.turn_claimed or self.turn_lock.locked()

    def claim_turn(self) -> bool:
        """Reserve the project's single turn slot. False when it is taken."""
        if self.turn_busy:
            return False
        self.turn_claimed = True
        return True

    def release_turn(self) -> None:
        self.turn_claimed = False

    async def run_exclusive_turn(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Run one agentic turn under the project-wide lock, or refuse.

        For callers holding an HTTP request open. Refusing rather than queueing
        is deliberate: a request parked behind a forty-minute mission returns a
        gateway timeout, and the button that sent it has no way to say "still
        waiting". Routes that skipped this entirely — design propose and
        implement did — could run a second agent against the working tree a
        CODE turn was already editing.

        The turn is registered as :attr:`active_turn` for the same reason a
        socket turn is: Stop has to be able to reach it from anywhere.
        """
        if not self.claim_turn():
            raise TurnBusy(
                "Another D[Ai]NO turn is already running for this project — "
                "wait for it to finish, or stop it there."
            )
        task: asyncio.Task[T] = asyncio.ensure_future(self._locked_turn(factory))
        # Fires even if the task is cancelled before its first step, which a
        # release inside the coroutine would miss — and a leaked claim would
        # wedge every later turn.
        task.add_done_callback(lambda _: self.release_turn())
        previous, self.active_turn = self.active_turn, task
        try:
            return await task
        finally:
            if self.active_turn is task:
                self.active_turn = previous

    async def _locked_turn(self, factory: Callable[[], Awaitable[T]]) -> T:
        async with self.turn_lock:
            return await factory()

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
            runs=WorkspaceRunApplicationService(context, missions, workbench, turn_lock=turn_lock),
            links=LinkStore(context.database, workbench),
            lsp=PooledLSPAdapter(context.root),
            tests=TestService(context.root),
            debugger=DebugManager(context.root),
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
        # Language servers are child processes; leaving them behind would leak a
        # pyright per restart.
        self._close_language_servers()
        # A test run outliving the server would keep writing reports nobody
        # will read, against a working tree that may be changing.
        self.tests.cancel()
        # A debuggee is a child process holding the working tree open, and a
        # paused one holds it open forever.
        self._stop_debugger()
        # Stopping the server mid-turn must not leave sleep inhibited.
        self.missions.attention.shutdown()

    def _stop_debugger(self) -> None:
        """Terminate the debuggee, from sync shutdown code."""
        import asyncio
        import contextlib

        if not self.debugger.running:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.debugger.stop())
            return
        with contextlib.suppress(Exception):
            asyncio.run(self.debugger.stop())

    def _close_language_servers(self) -> None:
        """Stop every language server, from sync shutdown code.

        ``shutdown`` is called from lifespan teardown and from the TUI, only one
        of which has a running loop, so the close is dispatched whichever way
        applies rather than assuming.
        """
        import asyncio
        import contextlib

        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            loop.create_task(self.lsp.close())
            return
        with contextlib.suppress(Exception):
            asyncio.run(self.lsp.close())
