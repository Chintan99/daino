"""Persistent, responsive Vasuki engineering workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from typing import Any

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher

from vasuki.application import (
    CheckpointApplicationService,
    DeploymentApplicationService,
    MissionApplicationService,
    ProjectContext,
    ProviderApplicationService,
    QAApplicationService,
    RepositoryApplicationService,
    SettingsApplicationService,
    VerificationApplicationService,
)
from vasuki.events import (
    AgentRoleChanged,
    ApprovalRequested,
    ApprovalResolved,
    CheckpointCreated,
    DeploymentFailed,
    DeploymentProgress,
    DeploymentStarted,
    DeploymentVerified,
    FileChanged,
    MissionCompleted,
    MissionCreated,
    MissionEvent,
    MissionFailed,
    MissionStarted,
    ModelSelected,
    ModelStreamChunk,
    RollbackCompleted,
    RollbackStarted,
    TaskCompleted,
    TaskStarted,
    TestsCompleted,
    TestsStarted,
    TodoUpdated,
    ToolCompleted,
    ToolFailed,
    ToolProgress,
    ToolStarted,
)
from vasuki.git import GitClient
from vasuki.observability import collect_stats
from vasuki.playbooks import PlaybookLoader
from vasuki.schemas import InteractionMode, MissionStatus, ProjectMode, QAReport, TodoItem
from vasuki.tui.screens.views import (
    ApprovalsView,
    CheckpointsView,
    DeploymentsView,
    DiffView,
    FilesView,
    HelpView,
    LogsView,
    MissionsView,
    PlaybooksView,
    ProvidersView,
    QAView,
    RepositoryView,
    SettingsView,
    TestsView,
)
from vasuki.tui.widgets import (
    ApprovalModal,
    CommandPalette,
    ContextStrip,
    ConversationView,
    ModelSelector,
    NavigationTab,
    NavigationTabs,
    PromptInput,
    TaskChecklist,
    VasukiHeader,
    VasukiHintBar,
)

#: Views the tab bar promotes are declared in ``NavigationTabs``; every view
#: below stays reachable through the command palette and slash commands.
SECONDARY_VIEWS: tuple[str, ...] = (
    "repository-view",
    "approvals-view",
    "checkpoints-view",
    "playbooks-view",
    "deployments-view",
    "providers-view",
    "settings-view",
    "help-view",
)

INTERACTION_MODES: tuple[InteractionMode, ...] = (
    InteractionMode.PLAN,
    InteractionMode.ASK,
    InteractionMode.SESSION,
    InteractionMode.FULL,
)

MODE_DESCRIPTIONS: dict[InteractionMode, str] = {
    InteractionMode.PLAN: "read-only planning; no implementation or deployment",
    InteractionMode.ASK: "routine repository work is allowed; risky commands ask first",
    InteractionMode.SESSION: "approval-gated agent commands are allowed for this session",
    InteractionMode.FULL: "normal in-scope work and mission gates run without prompts",
}


class WorkspaceScreen(Screen[None]):
    BINDINGS = [
        ("ctrl+p", "command_palette", "Commands"),
        Binding("shift+tab", "cycle_interaction_mode", "Mode", priority=True),
        Binding(
            "ctrl+tab",
            "cycle_interaction_mode",
            "Mode (legacy)",
            show=False,
            priority=True,
        ),
        ("ctrl+n", "new_session", "New mission"),
        ("ctrl+o", "open_view('files-view')", "Files"),
        ("ctrl+m", "model_selector", "Model"),
        ("ctrl+r", "open_view('missions-view')", "Resume"),
        ("ctrl+t", "run_tests", "Tests"),
        ("ctrl+d", "open_view('changes-view')", "Diff"),
        ("ctrl+l", "open_view('logs-view')", "Logs"),
        ("ctrl+i", "toggle_context", "Context"),
        ("ctrl+c", "cancel_work", "Cancel"),
        ("ctrl+q", "safe_quit", "Quit"),
        ("question_mark", "open_view('help-view')", "Help"),
    ]

    def __init__(self, context: ProjectContext) -> None:
        super().__init__()
        self.context = context
        self.missions = MissionApplicationService(context)
        self.repository = RepositoryApplicationService(context)
        self.providers = ProviderApplicationService(context)
        self.qa = QAApplicationService(context, self.missions)
        self.verification = VerificationApplicationService(context)
        self.deployments = DeploymentApplicationService(context)
        self.settings = SettingsApplicationService(context)
        self.checkpoints = CheckpointApplicationService(context)
        self.session_id = ""
        self.active_mission_id: str | None = None
        self.active_role = "Idle"
        self.active_status = "Ready"
        self.active_model = ""
        self.active_provider = ""
        self.interaction_mode = InteractionMode.ASK
        self.provider_health_status: dict[str, bool] = {}
        self._pending_approvals: set[tuple[str, str]] = set()
        self._resolved_approvals: set[tuple[str, str]] = set()
        self._question_missions: set[str] = set()
        self._chat_running = False
        self._event_subscription: Any = None
        self._context_user_hidden = False
        self._refresh_pending = False
        self._cached_branch = ""
        self._branch_checked_at = 0.0
        self._status_updated_at = 0.0
        self._last_usage: tuple[int, float] = (0, 0.0)
        self._last_qa_report: QAReport | None = None
        self._home = str(Path.home())

    def compose(self) -> ComposeResult:
        yield VasukiHeader(id="top-header")
        yield NavigationTabs(id="nav-tabs")
        with Horizontal(id="workspace-body"):
            with ContentSwitcher(initial="chat-view", id="main-workspace"):
                yield ConversationView(id="chat-view")
                yield MissionsView(self.missions)
                yield QAView()
                yield RepositoryView(self.repository)
                yield FilesView(self.repository)
                yield DiffView(self.repository)
                yield TestsView()
                yield ApprovalsView(self.missions)
                yield CheckpointsView(self.checkpoints)
                yield PlaybooksView(self.context.root)
                yield DeploymentsView(self.deployments)
                yield ProvidersView(self.providers)
                yield SettingsView(self.settings)
                yield LogsView(self.context.root)
                yield HelpView()
            yield TaskChecklist(id="task-checklist", classes="hidden-panel")
        yield ContextStrip(id="context-strip")
        yield PromptInput()
        yield VasukiHintBar(id="hint-bar")

    async def on_mount(self) -> None:
        theme = self.context.settings.tui.theme
        self.app.theme = {
            "dark": "vasuki-dark",
            "light": "textual-light",
            "system": "textual-ansi",
        }[theme]
        self.set_class(theme == "light", "theme-light")
        self._event_subscription = self.context.events.subscribe(self._receive_event)
        # A fresh session every launch. Resuming the last one reloaded its whole
        # transcript and, worse, fed it back as conversation history on the next
        # prompt — paying for context from a conversation that is already over.
        # Earlier sessions stay in the database and are still browsable.
        self.session_id = self.missions.create_session()
        self.interaction_mode = self.missions.interaction_mode(self.session_id)
        conversation = self.query_one("#chat-view", ConversationView)
        conversation.set_environment(
            self._configured_provider(),
            self.context.settings.runtime.default,
        )
        await conversation.load_messages(self.missions.messages(self.session_id))
        resumable = self.missions.resumable_tasks()
        if resumable:
            item = resumable[0]
            completed = len(item.completed_steps)
            total = completed + len(item.pending_steps) + bool(item.current_step)
            remaining = ", ".join(item.pending_steps) or item.current_step or "inspect task"
            await conversation.add_message(
                "Resumable task detected:\n"
                f"{item.interpreted_goal or item.original_request}\n"
                f"Progress: {completed}/{total}\n"
                f"Last action: {item.last_action or 'none'}\n"
                f"Remaining: {remaining}\n"
                + (
                    f"Use `/resume {item.mission_id}` to continue."
                    if item.mission_id
                    else "Use `/tasks` to inspect it."
                ),
                kind="status",
            )
        self._load_checklist()
        latest_qa = self.qa.latest()
        if latest_qa is not None:
            self._show_qa_report(latest_qa)
        self._load_references()
        self._update_context_files()
        self._update_header()
        self._update_status()
        self.query_one("#hint-bar", VasukiHintBar).set_state(
            submit="enter",
            mode=self.interaction_mode.value,
        )
        self.query_one(PromptInput).focus_prompt()
        # Header and status refreshes shell out to Git and query the database, so they
        # are coalesced onto a timer instead of running once per streamed event.
        self.set_interval(0.25, self._flush_refresh)
        self.provider_health()

    def _configured_provider(self) -> str:
        profile = self.context.settings.models.get(self.providers.routable_profile())
        return profile.provider if profile else "offline"

    def on_unmount(self) -> None:
        if self._event_subscription is not None:
            self._event_subscription.close()

    @work(exclusive=True, group="references", thread=True)
    def _load_references(self) -> None:
        """Collect @-completions off the UI thread.

        ``repository.files()`` builds the whole index when no cache exists, which
        on a large repository is many seconds of parsing. Doing that inside
        ``on_mount`` meant the window did not appear until it finished, which is
        indistinguishable from the app failing to start.
        """
        references: list[str] = []
        try:
            references.extend(f"@file:{item.path}" for item in self.repository.files())
        except Exception as exc:
            references.append(f"@file:index-unavailable-{type(exc).__name__}")
        references.extend(f"@mission:{item.id}" for item in self.missions.list_missions(30))
        references.extend(
            f"@playbook:{item.name}" for item in PlaybookLoader(self.context.root).list()
        )
        self.app.call_from_thread(self.query_one(PromptInput).set_references, references)

    def _update_context_files(self) -> None:
        if not self.session_id:
            return
        paths = self.missions.context_files(self.session_id)
        self.query_one("#context-strip", ContextStrip).set_files(paths)

    def _load_checklist(self) -> None:
        if not self.session_id:
            return
        self.query_one("#task-checklist", TaskChecklist).set_todos(
            self.missions.session_todos(self.session_id)
        )

    def _set_activity(self, state: str, detail: str = "") -> None:
        self.query_one("#task-checklist", TaskChecklist).set_activity(state, detail)

    @staticmethod
    def _role_activity(role: str) -> str:
        return {
            "architect": "thinking",
            "planner": "planning",
            "builder": "building",
            "debugger": "inspecting",
            "reviewer": "inspecting",
            "tester": "verifying",
            "summarizer": "thinking",
            "deployer": "building",
        }.get(role.casefold(), "thinking")

    @staticmethod
    def _tool_activity(tool: str) -> str:
        lowered = tool.casefold()
        if any(item in lowered for item in ("test", "verify", "lint", "typecheck", "build")):
            return "verifying"
        if any(item in lowered for item in ("write", "replace", "edit", "delete", "patch")):
            return "building"
        if any(item in lowered for item in ("read", "search", "grep", "glob", "list", "memory")):
            return "inspecting"
        return "thinking"

    def _receive_event(self, event: MissionEvent) -> None:
        if not self.is_mounted:
            return
        try:
            self.app.call_from_thread(self.call_later, self._render_event, event)
        except RuntimeError:
            self.call_later(self._render_event, event)

    async def _render_event(self, event: MissionEvent) -> None:
        conversation = self.query_one("#chat-view", ConversationView)
        context = self.query_one("#context-strip", ContextStrip)
        now = event.timestamp.strftime("%H:%M:%S")
        if isinstance(event, MissionCreated) and event.mode == ProjectMode.DIRECT.value:
            # Questions create a direct-mode mission for auditing only. Adopting it as
            # the active mission would point /build, /review, and the diff view at it.
            self._question_missions.add(event.mission_id or "")
        if event.mission_id and event.mission_id not in self._question_missions:
            self.active_mission_id = event.mission_id

        if isinstance(event, MissionCreated):
            # A plain question creates a direct-mode mission purely for auditing;
            # announcing it in the transcript is noise between the prompt and answer.
            if event.mode == ProjectMode.DIRECT.value:
                self._set_activity("thinking", "understanding request")
                context.add_activity(f"{now}  Question {event.mission_id}")
            else:
                self.active_status = "Planning"
                self._set_activity("planning", "shaping the task")
                await conversation.add_message(
                    f"Created {event.mission_id} in {event.mode} mode.",
                    kind="status",
                    role="planner",
                )
                context.set_mission(event.mission_id or "", "Planning")
        elif isinstance(event, AgentRoleChanged):
            self.active_role = event.role.title()
            self._set_activity(self._role_activity(event.role), f"{event.role} active")
            context.add_activity(f"{now}  {event.role.title()} active")
        elif isinstance(event, ModelSelected):
            self.active_model = event.profile
            self.active_provider = event.provider
            context.add_activity(f"{now}  Model {event.profile}")
        elif isinstance(event, ModelStreamChunk):
            await conversation.append_stream(event.content, role=event.role)
        elif isinstance(event, MissionStarted):
            self.active_status = "Running"
            self._set_activity("building", "mission running")
            await conversation.add_message(
                f"Worktree: {event.workspace}\nBranch: {event.branch}",
                kind="status",
                role="builder",
            )
            context.set_mission(event.mission_id or "", "Running")
        elif isinstance(event, TaskStarted):
            self.active_status = "Running"
            self._set_activity("building", event.title)
            self.missions.update_session_todo(
                self.session_id,
                event.title,
                "in_progress",
                mission_id=event.mission_id,
            )
            await conversation.add_message(
                f"{event.title}",
                kind="agent",
                role=self.active_role or "builder",
            )
            context.add_activity(f"{now}  Started {event.title[:24]}")
        elif isinstance(event, TaskCompleted):
            self.missions.update_session_todo(
                self.session_id,
                event.title,
                "completed",
                mission_id=event.mission_id,
            )
            context.add_activity(f"{now}  Completed {event.title[:22]}")
        elif isinstance(event, TodoUpdated):
            if event.session_id == self.session_id:
                self.query_one("#task-checklist", TaskChecklist).set_todos(
                    [TodoItem.model_validate(item) for item in event.todos]
                )
        elif isinstance(event, ToolStarted):
            self._set_activity(self._tool_activity(event.tool), event.summary)
            context.add_activity(f"{now}  {event.summary[:30]}")
            conversation.update_pending(f"{event.tool.removeprefix('agent.')} {event.summary}")
        elif isinstance(event, ToolProgress):
            context.add_activity(f"{now}  {event.summary[:30]}")
        elif isinstance(event, ToolCompleted):
            await conversation.add_message(
                f"{event.summary} ({event.duration_seconds:.2f}s)",
                kind="tool",
                metadata=event.payload(),
            )
        elif isinstance(event, ToolFailed):
            self._set_activity("failed", event.error)
            await conversation.add_message(
                f"{event.tool}: {event.error}",
                kind="error",
                metadata=event.payload(),
            )
        elif isinstance(event, FileChanged):
            self._set_activity("building", event.path)
            # Show the change itself when the event carries one. "Replace
            # cars.html" says a file moved; the diff says what the agent did.
            await conversation.add_message(
                event.diff or f"{event.action.title()} {event.path}",
                kind="diff" if event.diff else "tool",
                metadata=event.payload(),
            )
            self._refresh_diff()
        elif isinstance(event, TestsStarted):
            self.active_status = "Verifying"
            self._set_activity("verifying", "running checks")
            await conversation.add_message(
                "Running:\n" + "\n".join(event.commands),
                kind="test",
                role="tester",
            )
            context.set_tests("Running…")
        elif isinstance(event, TestsCompleted):
            label = (
                f"{event.passed_count} passed"
                if event.passed
                else f"{event.passed_count} passed, {event.failed_count} failed"
            )
            await conversation.add_message(
                label,
                kind="test" if event.passed else "error",
                role="tester",
                metadata=event.payload(),
            )
            context.set_tests(label)
            self.active_status = (
                "Failed"
                if not event.passed
                else ("Running" if self._chat_running or self.active_mission_id else "Ready")
            )
            self._set_activity(
                "verifying" if event.passed else "failed",
                label,
            )
        elif isinstance(event, CheckpointCreated):
            # The automatic pre-edit checkpoint fires on every prompt. Announcing
            # it each time is noise around the answer the user actually asked
            # for; it still appears in the Checkpoints view and in the activity
            # strip, which is where someone looking to restore one would go.
            context.add_activity(f"{now}  Checkpoint {event.description}"[:60])
            self.query_one("#checkpoints-view", CheckpointsView).refresh_data()
        elif isinstance(event, ApprovalRequested):
            approval_key = (event.mission_id or "", event.category)
            if (
                not event.mission_id
                or approval_key in self._pending_approvals
                or approval_key in self._resolved_approvals
            ):
                return
            if (
                self.interaction_mode == InteractionMode.PLAN
                and event.category == "mission_execution"
            ):
                self.active_status = "Planned"
                await conversation.add_message(
                    "Plan ready. Switch mode with Shift+Tab or use /mode ask, then /build.",
                    kind="status",
                    role="planner",
                )
                self.request_refresh()
                return
            self._pending_approvals.add(approval_key)
            await conversation.add_message(
                (
                    f"{event.subject}\nRisk: {event.risk}\nAuto-approved by Full access."
                    if self.interaction_mode == InteractionMode.FULL
                    else f"{event.subject}\nRisk: {event.risk}"
                ),
                kind="approval",
            )
            if self.interaction_mode == InteractionMode.FULL:
                self._pending_approvals.discard(approval_key)
                self._apply_approval_result(event, (True, "full"))
                return
            self.active_status = "Awaiting approval"
            if event.category == "mission_changes":
                self._refresh_diff()
                self.action_open_view("changes-view")
            self._show_approval(event, reserved=True)
        elif isinstance(event, ApprovalResolved):
            self.active_status = "Approved" if event.approved else "Blocked"
            await conversation.add_message(
                "Approved" if event.approved else "Rejected",
                kind="approval" if event.approved else "error",
            )
        elif isinstance(event, MissionCompleted):
            conversation.finish_stream()
            self.active_status = "Completed"
            self._set_activity("completed", "all work verified")
            await conversation.add_message(
                f"Mission completed.\nEvidence: {event.evidence_path}",
                kind="summary",
            )
            context.set_mission(event.mission_id or "", "Completed")
            self._refresh_missions()
            self._refresh_diff()
        elif isinstance(event, MissionFailed):
            conversation.finish_stream()
            self.active_status = "Failed"
            self._set_activity("failed", "needs attention")
            # The worker that invoked the service renders the actionable error.
            # Rendering it here as well races that exception handler and creates
            # two red cards for the same failure.
            context.set_mission(event.mission_id or "", "Failed")
            self._refresh_missions()
        elif isinstance(event, DeploymentStarted):
            self._set_activity("building", f"deploying {event.target}")
            await conversation.add_message(
                f"{event.action.title()} {event.target}",
                kind="deployment",
            )
        elif isinstance(event, DeploymentProgress):
            self._set_activity("building", event.stage)
            context.add_activity(f"{now}  Deploy: {event.stage[:24]}")
        elif isinstance(event, DeploymentVerified):
            self._set_activity(
                "completed" if event.healthy else "failed",
                f"{event.target} {'healthy' if event.healthy else 'unhealthy'}",
            )
            await conversation.add_message(
                f"{event.target}: {'healthy' if event.healthy else 'unhealthy'}",
                kind="deployment" if event.healthy else "error",
            )
        elif isinstance(event, DeploymentFailed):
            self._set_activity("failed", event.target)
            await conversation.add_message(
                f"{event.target}: {event.error}",
                kind="error",
            )
        elif isinstance(event, RollbackStarted):
            await conversation.add_message(
                f"Rollback started for {event.target}",
                kind="deployment",
            )
        elif isinstance(event, RollbackCompleted):
            await conversation.add_message(
                f"Rollback completed for {event.target}",
                kind="deployment",
            )
        self.request_refresh()

    def request_refresh(self) -> None:
        """Mark the header and status bar dirty; the interval timer applies it."""
        self._refresh_pending = True

    def _flush_refresh(self) -> None:
        # Long operations can outlive the screen; never query a torn-down tree.
        if not self._refresh_pending or not self.is_mounted:
            return
        self._update_header()
        now = monotonic()
        if now - self._status_updated_at < 1.0:
            # Keep the refresh queued. Model chunks often arrive immediately
            # after mount, inside this throttle window, and their usage record
            # is only committed when the stream finishes.
            return
        self._refresh_pending = False
        self._status_updated_at = now
        self._update_status()

    def _show_approval(
        self,
        event: ApprovalRequested,
        *,
        reserved: bool = False,
    ) -> None:
        mission_id = event.mission_id
        if not mission_id:
            return
        approval_key = (mission_id, event.category)
        if self.interaction_mode == InteractionMode.FULL:
            self._apply_approval_result(event, (True, "full"))
            return
        if not reserved:
            if approval_key in self._pending_approvals or approval_key in self._resolved_approvals:
                return
            self._pending_approvals.add(approval_key)
        handled = False

        def resolved(result: tuple[bool, str] | None) -> None:
            nonlocal handled
            if handled:
                return
            handled = True
            self._pending_approvals.discard(approval_key)
            if result is None:
                return
            self._apply_approval_result(event, result)

        self.app.push_screen(
            ApprovalModal(
                title="Mission Plan Approval",
                subject=event.subject,
                risk=event.risk,
                details=(
                    "Implementation will create an isolated Git workspace, modify only "
                    "planned files, run verification, and perform independent review."
                ),
            ),
            resolved,
        )

    def _apply_approval_result(
        self,
        event: ApprovalRequested,
        result: tuple[bool, str],
    ) -> None:
        """Persist one decision and continue the operation it was guarding."""
        mission_id = event.mission_id
        if not mission_id:
            return
        approval_key = (mission_id, event.category)
        self._resolved_approvals.add(approval_key)
        approved, scope = result
        if approved and scope == "session":
            self._set_interaction_mode(InteractionMode.SESSION)
        if event.category == "mission_changes":
            if approved:
                self.set_timer(0.05, lambda: self.finalize_changes(mission_id, scope))
            else:
                try:
                    self.missions.approve(
                        mission_id,
                        approved=False,
                        scope=scope,
                        category=event.category,
                    )
                except Exception as exc:
                    self.notify(str(exc), severity="error")
            return
        try:
            self.missions.approve(
                mission_id,
                approved=approved,
                scope=scope,
                category=event.category,
            )
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        if approved and event.category == "mission_execution":
            # Full mode may resolve during the final milliseconds of planning;
            # defer execution so its exclusive worker does not cancel the planner.
            self.set_timer(0.05, lambda: self.execute_mission(mission_id))

    def _current_branch(self) -> str:
        """Return the branch name, re-running Git at most every few seconds.

        Empty when the project is not a Git repository: the header omits the
        token entirely rather than spending a slot on an absence.
        """
        now = monotonic()
        if now - self._branch_checked_at < 5.0:
            return self._cached_branch
        try:
            branch = GitClient(self.context.root).current_branch() or "detached"
        except Exception:
            branch = ""
        self._cached_branch = branch
        self._branch_checked_at = now
        return branch

    def _update_header(self, *, tokens: int = -1, cost: float = -1.0) -> None:
        # active_model holds a profile name; the header shows the model it points
        # at, which is what the user actually recognises.
        profile_name = self.active_model or self.providers.routable_profile()
        profile = self.context.settings.models.get(profile_name)
        provider = profile.provider if profile else self.active_provider
        model = profile.model if profile else ""
        if tokens < 0:
            tokens, cost = self._last_usage
        self.query_one("#top-header", VasukiHeader).set_state(
            project=str(self.context.root),
            home=self._home,
            branch=self._current_branch(),
            model=model or "not configured",
            provider=provider or "offline",
            runtime=self.context.settings.runtime.default,
            interaction_mode=self.interaction_mode.value,
            status=self.active_status,
            connected=self.provider_health_status.get(provider),
            tokens=tokens,
            cost=cost,
        )

    def _update_status(self) -> None:
        try:
            with self.context.database.session() as session:
                stats = collect_stats(session, self.active_mission_id)
        except Exception:
            stats = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0}
        statuses = self.repository.git_status()
        failures = int(stats.get("verification_failures", 0))
        approvals = 1 if self.active_status == "Awaiting approval" else 0
        self._last_usage = (
            int(stats["input_tokens"]) + int(stats["output_tokens"]),
            float(stats["estimated_cost"]),
        )
        self._update_header(tokens=self._last_usage[0], cost=self._last_usage[1])
        self.query_one("#context-strip", ContextStrip).set_approvals(approvals)
        self.query_one("#nav-tabs", NavigationTabs).set_badges(
            {
                "missions-view": str(int(stats.get("missions", 0))),
                "qa-view": self._qa_badge(),
                "changes-view": str(len(statuses)),
                "tests-view": f"{failures} failed" if failures else "",
            }
        )

    def _refresh_missions(self) -> None:
        self.query_one("#missions-view", MissionsView).refresh_data()

    def _qa_badge(self) -> str:
        if self._last_qa_report is None:
            return ""
        if self._last_qa_report.status == "running":
            return "running"
        failures = sum(item.status == "failed" for item in self._last_qa_report.checks)
        failures += sum(item.status == "failed" for item in self._last_qa_report.specialists)
        return f"{failures} failed" if failures else "done"

    def _refresh_diff(self) -> None:
        self.query_one("#changes-view", DiffView).refresh_data(self.active_mission_id)

    def set_active_mission(self, mission_id: str) -> None:
        self.active_mission_id = mission_id
        session_id = self.missions.session_for_mission(mission_id)
        if session_id and session_id != self.session_id:
            self.session_id = session_id
            self.interaction_mode = self.missions.interaction_mode(session_id)
            self.query_one("#hint-bar", VasukiHintBar).set_mode(self.interaction_mode.value)
            asyncio.create_task(
                self.query_one("#chat-view", ConversationView).load_messages(
                    self.missions.messages(session_id)
                )
            )
            self._update_context_files()
            self._load_checklist()
        details = self.missions.mission_details(mission_id)
        self.active_status = str(details["mission"]["status"]).replace("_", " ").title()
        self.query_one("#context-strip", ContextStrip).set_mission(mission_id, self.active_status)
        self._refresh_diff()
        self.request_refresh()

    def on_navigation_tab_selected(self, event: NavigationTab.Selected) -> None:
        self.action_open_view(event.view_id)

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        await self.execute_command(event.value)

    def on_prompt_input_cancelled(self, _: PromptInput.Cancelled) -> None:
        self.action_cancel_work()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-qa":
            self.run_qa()
        elif event.button.id == "refresh-qa-history":
            self._refresh_qa_history()
        elif event.button.id in {"run-targeted", "run-full", "run-failed"}:
            self.run_tests()
        elif event.button.id == "toggle-file-context":
            view = self.query_one("#files-view", FilesView)
            if not view.selected_path:
                self.notify("Select a file first.", severity="warning")
                return
            try:
                attached = self.missions.toggle_context_file(
                    self.session_id,
                    view.selected_path,
                )
                self._update_context_files()
                self.notify(
                    f"{'Added' if attached else 'Removed'} {view.selected_path} "
                    f"{'to' if attached else 'from'} agent context."
                )
            except Exception as exc:
                self.notify(str(exc), severity="error")
        elif event.button.id == "apply-setting":
            settings_view = self.query_one("#settings-view", SettingsView)
            key, value = settings_view.pending_change()
            if not key or not value:
                self.notify("Enter a dotted setting key and YAML value.", severity="warning")
                return
            try:
                self.settings.set(key, value)
                settings_view.refresh_data()
                self.notify(f"Updated {key}")
            except Exception as exc:
                self.notify(f"Invalid setting: {exc}", severity="error")
        elif event.button.id == "save-provider":
            provider_view = self.query_one("#providers-view", ProvidersView)
            values = provider_view.pending_provider()
            if not all(values[key] for key in ("name", "provider_type", "base_url", "model")):
                self.notify(
                    "Provider name, type, base URL, and model are required.",
                    severity="warning",
                )
                return
            self.save_provider(values)
        elif event.button.id == "refresh-provider-models":
            self.query_one("#providers-view", ProvidersView).load_openrouter_models()

    async def execute_command(self, raw: str) -> None:
        if raw.startswith("!"):
            # The user is running their own command, not asking the agent to.
            command = raw[1:].strip()
            if command:
                self.run_user_shell(command)
            else:
                self.notify("Usage: !<command>, for example !git status", severity="warning")
            return
        if not raw.startswith("/"):
            # A bare prompt goes to the agent, which decides whether the request
            # was a question to answer or a change to make. Use /ask to force an
            # answer without touching the repository.
            await self._echo_user(raw)
            if self.interaction_mode == InteractionMode.PLAN:
                self.plan_mission(raw)
            else:
                self.run_chat_agent(raw)
            return
        command, _, arguments = raw.partition(" ")
        arguments = arguments.strip()
        view_commands = {
            "/help": "help-view",
            "/missions": "missions-view",
            "/qa": "qa-view",
            "/files": "files-view",
            "/diff": "changes-view",
            "/checkpoints": "checkpoints-view",
            "/playbooks": "playbooks-view",
            "/provider": "providers-view",
            "/settings": "settings-view",
            "/logs": "logs-view",
            "/status": "chat-view",
        }
        if command in view_commands and not (command in {"/provider", "/qa"} and arguments):
            self.action_open_view(view_commands[command])
            if command == "/files" and arguments:
                self.query_one("#files-view", FilesView).refresh_data(arguments)
            if command == "/status":
                await self.query_one("#chat-view", ConversationView).add_message(
                    f"Project: {self.context.root}\n"
                    f"Mission: {self.active_mission_id or 'none'}\n"
                    f"Status: {self.active_status}\n"
                    f"Runtime: {self.context.settings.runtime.default}\n"
                    f"Mode: {self.interaction_mode.value}",
                    kind="status",
                )
            return
        if command == "/clear":
            await self.query_one("#chat-view", ConversationView).clear_visible()
        elif command == "/new":
            await self.new_session(arguments or "New conversation")
        elif command == "/mode":
            if not arguments:
                self.notify(
                    f"Mode is {self.interaction_mode.value}. "
                    "Use /mode plan|ask|session|full or Shift+Tab."
                )
            else:
                try:
                    self._set_interaction_mode(InteractionMode(arguments.casefold()))
                except ValueError:
                    self.notify("Mode must be plan, ask, session, or full.", severity="warning")
        elif command == "/ask":
            if not arguments:
                self.notify("Usage: /ask <question>", severity="warning")
            else:
                await self._echo_user(arguments)
                self.ask_question(arguments)
        elif command in {"/plan", "/run"}:
            if not arguments:
                self.notify(f"Usage: {command} <instruction>", severity="warning")
            else:
                await self._echo_user(arguments)
                self.plan_mission(arguments)
        elif command == "/team":
            if not arguments:
                self.notify("Usage: /team <instruction>", severity="warning")
            elif self.interaction_mode == InteractionMode.PLAN:
                self.notify("Team execution is disabled in Plan mode.", severity="warning")
            else:
                await self._echo_user(arguments)
                self.run_team(arguments)
        elif command == "/build":
            if arguments:
                await self._echo_user(arguments)
                self.plan_mission(arguments)
            elif self.active_mission_id:
                if self.interaction_mode == InteractionMode.PLAN:
                    self.notify(
                        "Switch to Ask, Session, or Full mode before building.",
                        severity="warning",
                    )
                else:
                    self._request_execution_approval(self.active_mission_id)
            else:
                self.notify("No active mission", severity="warning")
        elif command == "/test":
            self.run_tests(arguments)
        elif command == "/qa":
            if arguments.casefold() == "run":
                self.run_qa()
            else:
                self.notify("Usage: /qa or /qa run", severity="warning")
        elif command == "/review":
            if self.active_mission_id:
                self.run_review(self.active_mission_id)
            else:
                self.notify("No active mission to review", severity="warning")
        elif command == "/resume":
            mission_id = arguments or self.active_mission_id
            if mission_id:
                self.set_active_mission(mission_id)
                details = self.missions.mission_details(mission_id)
                if details["mission"]["status"] == MissionStatus.AWAITING_APPROVAL.value:
                    self._request_execution_approval(mission_id)
                elif arguments:
                    self.execute_mission(mission_id)
                self.action_open_view("chat-view")
            else:
                self.action_open_view("missions-view")
        elif command == "/tasks":
            await self.query_one("#chat-view", ConversationView).add_message(
                self.missions.task_command(),
                kind="status",
            )
        elif command == "/memory":
            try:
                response = self.missions.memory_command(arguments, self.session_id)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
            else:
                await self.query_one("#chat-view", ConversationView).add_message(
                    response,
                    kind="status",
                )
        elif command == "/cancel":
            self.action_cancel_work()
        elif command == "/checkpoint":
            self.create_checkpoint(arguments or "Manual checkpoint")
        elif command == "/restore":
            if arguments:
                self.confirm_restore(arguments)
            else:
                self.action_open_view("checkpoints-view")
        elif command == "/model":
            if arguments:
                self.select_model(arguments)
            else:
                self.action_model_selector()
        elif command == "/provider" and arguments:
            self.test_provider(arguments)
        elif command == "/runtime":
            if arguments:
                try:
                    self.settings.set_runtime(arguments, persist=False)
                    self.request_refresh()
                    self.notify(
                        f"Runtime set to {arguments} for this session. Use Settings to persist it."
                    )
                except Exception as exc:
                    self.notify(str(exc), severity="error")
            else:
                self.action_open_view("settings-view")
        elif command == "/index":
            self.reindex()
        elif command == "/deploy":
            parts = arguments.split(maxsplit=1)
            if len(parts) != 2:
                self.notify(
                    "Usage: /deploy inspect|plan|apply|verify|rollback <target>",
                    severity="warning",
                )
            else:
                if self.interaction_mode == InteractionMode.PLAN and parts[0] in {
                    "apply",
                    "rollback",
                }:
                    self.notify("Deployments are disabled in Plan mode.", severity="warning")
                else:
                    self.run_deployment(parts[0], parts[1])
        elif command in {"/bye", "/quit", "/exit"}:
            self.action_safe_quit()
        else:
            self.notify(f"Unknown command: {command}. Use /help.", severity="warning")

    async def new_session(self, title: str = "New conversation") -> None:
        self.session_id = self.missions.create_session(title)
        self.interaction_mode = InteractionMode.ASK
        self.query_one("#hint-bar", VasukiHintBar).set_mode(self.interaction_mode.value)
        self.active_mission_id = None
        self.active_status = "Ready"
        self._set_activity("idle")
        await self.query_one("#chat-view", ConversationView).load_messages([])
        self._load_checklist()
        self._update_context_files()
        self.action_open_view("chat-view")
        self.request_refresh()

    async def _echo_user(self, text: str) -> None:
        self.action_open_view("chat-view")
        await self.query_one("#chat-view", ConversationView).add_message(
            text,
            kind="user",
            follow=True,
        )

    @work(exclusive=True, group="mission")
    async def plan_mission(self, request: str) -> None:
        conversation = self.query_one("#chat-view", ConversationView)
        self.action_open_view("chat-view")
        self.active_status = "Planning"
        self._set_activity("planning", "shaping the task")
        self.request_refresh()
        try:
            mission, requirements, plan = await self.missions.plan(
                request,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
            )
            self.active_mission_id = mission.id
            tasks = "\n".join(
                f"{index}. {task.title} [{task.risk_level}]"
                for index, task in enumerate(plan.tasks, 1)
            )
            await conversation.add_message(
                f"{requirements.problem_statement}\n\n{tasks}",
                kind="plan",
                role="architect",
                metadata={
                    "requirements": requirements.model_dump(mode="json"),
                    "tasks": [task.model_dump(mode="json") for task in plan.tasks],
                },
            )
            self._refresh_missions()
            self._set_activity("completed", "plan ready")
        except asyncio.CancelledError:
            if self.active_mission_id:
                self.missions.cancel(self.active_mission_id)
            raise
        except Exception as exc:
            self.active_status = "Failed"
            self._set_activity("failed", "planning stopped")
            await conversation.add_message(
                self._actionable_error(exc),
                kind="error",
            )
            self.notify("Planning failed", severity="error")
        finally:
            self.request_refresh()

    async def _approve_command(self, command: str, reason: str) -> tuple[bool, bool]:
        """Ask the user before the agent runs something outside the safe set.

        Returns (approved, remember). "Approve for this mission" is the modal's
        standing-permission answer, so the same kind of command stops asking for
        the rest of the session.
        """
        if self.interaction_mode in {InteractionMode.SESSION, InteractionMode.FULL}:
            return True, True
        if self.interaction_mode == InteractionMode.PLAN:
            return False, False
        network_request = "network" in reason or "internet" in reason
        answer = await self.app.push_screen_wait(
            ApprovalModal(
                title="Allow internet access?" if network_request else "Run command?",
                subject=command,
                risk="high" if network_request or "install" in reason else "medium",
                details=f"{reason}\n\nApprove once, or approve for this session.",
            )
        )
        if answer is None:
            return False, False
        approved, scope = answer
        if approved and scope == "session":
            self._set_interaction_mode(InteractionMode.SESSION)
        return approved, approved and scope != "once"

    @work(exclusive=True, group="shell")
    async def run_user_shell(self, command: str) -> None:
        """Run a ``!`` command. Its own worker group, so it never cancels a mission."""
        conversation = self.query_one("#chat-view", ConversationView)
        self.action_open_view("chat-view")
        try:
            await self.missions.run_shell(command, self.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await conversation.add_message(self._actionable_error(exc), kind="error")
        # The service persisted the command and its output; reload so the
        # transcript matches what is on disk.
        await conversation.load_messages(self.missions.messages(self.session_id))
        self._refresh_diff()

    @work(exclusive=True, group="mission")
    async def run_chat_agent(self, instruction: str) -> None:
        """Let the agent answer or edit, then show what it actually changed."""
        conversation = self.query_one("#chat-view", ConversationView)
        self.action_open_view("chat-view")
        previous_status = self.active_status
        turn_status = previous_status
        self._chat_running = True
        self.active_status = "Working"
        self._set_activity("thinking", "understanding request")
        self.request_refresh()
        await conversation.begin_pending("working…")
        started = monotonic()
        try:
            outcome = await self.missions.chat(
                instruction,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
                approve=self._approve_command,
            )
            # The service persisted the answer, each diff, and any verification
            # result, so reload rather than echo them a second time.
            await conversation.clear_pending()
            await conversation.sync_messages(self.missions.messages(self.session_id))
            if outcome.changed:
                # Only a turn that changed something becomes the active mission.
                # A question that happened to open an audit-only mission must not
                # steal /diff and /review from the mission actually in progress.
                self.active_mission_id = outcome.mission_id or self.active_mission_id
                self._refresh_diff()
                self.notify(f"{len(outcome.changed)} file(s) changed")
            if outcome.verified is False:
                turn_status = "Failed"
                self._set_activity("failed", "verification failed")
            elif outcome.verified is None and outcome.changed:
                turn_status = "Blocked"
                self._set_activity("completed", "changes need verification")
            else:
                self._set_activity("completed", "request complete")
        except asyncio.CancelledError:
            turn_status = "Cancelled"
            self._set_activity("idle")
            await conversation.add_message("cancelled", kind="status")
            raise
        except Exception as exc:
            turn_status = "Failed"
            self._set_activity("failed", "needs attention")
            await conversation.add_message(self._actionable_error(exc), kind="error")
        finally:
            self._chat_running = False
            conversation.finish_stream(monotonic() - started)
            await conversation.clear_pending()
            self.active_status = (
                turn_status
                if self.active_mission_id
                else ("Ready" if turn_status == previous_status else turn_status)
            )
            self.request_refresh()

    @work(exclusive=True, group="mission")
    async def run_team(self, instruction: str) -> None:
        conversation = self.query_one("#chat-view", ConversationView)
        self.action_open_view("chat-view")
        self.active_status = "Planning team"
        self._set_activity("planning", "assembling team")
        self.request_refresh()
        try:
            outcome = await self.missions.team(
                instruction,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
            )
            self.active_mission_id = outcome.mission_id or self.active_mission_id
            failed = [member for member in outcome.members if not member.success]
            self.active_status = "Failed" if failed else "Ready"
            self._set_activity(
                "failed" if failed else "completed",
                f"{len(failed)} member failures" if failed else "team complete",
            )
            # The service already wrote the roster and the result into the
            # session, so reload rather than echo them a second time.
            await conversation.load_messages(self.missions.messages(self.session_id))
            self._refresh_missions()
            if failed:
                self.notify(f"{len(failed)} team member(s) failed", severity="error")
        except asyncio.CancelledError:
            if self.active_mission_id:
                self.missions.cancel(self.active_mission_id)
            raise
        except Exception as exc:
            self.active_status = "Failed"
            self._set_activity("failed", "team stopped")
            await conversation.add_message(self._actionable_error(exc), kind="error")
            self.notify("Team run failed", severity="error")
        finally:
            self.request_refresh()

    @work(exclusive=True, group="mission")
    async def execute_mission(self, mission_id: str) -> None:
        self.active_mission_id = mission_id
        self._set_activity("building", "resuming mission")
        try:
            await self.missions.execute(
                mission_id,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
            )
        except asyncio.CancelledError:
            self.missions.cancel(mission_id)
            raise
        except Exception as exc:
            self._set_activity("failed", "mission stopped")
            await self.query_one("#chat-view", ConversationView).add_message(
                self._actionable_error(exc),
                kind="error",
            )

    @work(exclusive=True, group="mission")
    async def finalize_changes(self, mission_id: str, scope: str) -> None:
        try:
            await asyncio.to_thread(
                self.missions.approve_changes,
                mission_id,
                self.session_id,
                scope=scope,
            )
        except Exception as exc:
            await self.query_one("#chat-view", ConversationView).add_message(
                self._actionable_error(exc),
                kind="error",
            )

    @work(exclusive=True, group="chat")
    async def ask_question(self, question: str) -> None:
        """Answer a question without disturbing a mission running in parallel."""
        conversation = self.query_one("#chat-view", ConversationView)
        self.action_open_view("chat-view")
        previous_status = self.active_status
        checklist = self.query_one("#task-checklist", TaskChecklist)
        previous_activity = (checklist.activity_state, checklist.activity_detail)
        self.active_status = "Thinking"
        self._set_activity("thinking", "answering question")
        self.request_refresh()
        await conversation.begin_pending("thinking…")
        started = monotonic()
        try:
            await self.missions.ask(
                question,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
            )
            self._set_activity("completed", "answer ready")
        except asyncio.CancelledError:
            await conversation.add_message("answer cancelled", kind="status")
            raise
        except Exception as exc:
            self._set_activity("failed", "answer stopped")
            await conversation.add_message(self._actionable_error(exc), kind="error")
        finally:
            conversation.finish_stream(monotonic() - started)
            await conversation.clear_pending()
            self.active_status = previous_status if self.active_mission_id else "Ready"
            if self.active_mission_id and previous_status in {"Running", "Verifying"}:
                self._set_activity(*previous_activity)
            self.request_refresh()

    @work(exclusive=True, group="verification")
    async def run_tests(self, target: str = "") -> None:
        self.action_open_view("tests-view")
        self._set_activity("verifying", "running checks")
        commands = None
        if target and target not in {"targeted", "failed", "full"}:
            commands = [target]
        try:
            report = await self.verification.run(
                commands,
                mission_id=self.active_mission_id,
                approve=self._approve_command,
                gate=self.missions._session_gate(self.session_id),
            )
            self.query_one("#tests-view", TestsView).show_report(report)
            self._set_activity(
                "completed" if report.passed else "failed",
                "checks passed" if report.passed else "checks failed",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_activity("failed", "checks stopped")
            self.notify(self._actionable_error(exc), severity="error")

    @work(exclusive=True, group="qa")
    async def run_qa(self) -> None:
        """Run automated evidence collection and parallel read-only reviewers."""
        self.action_open_view("qa-view")
        previous_status = self.active_status
        self.active_status = "QA running"
        self._set_activity("verifying", "quality checks")
        self.request_refresh()
        try:
            report = await self.qa.run(
                profile_override=self.providers.session_profile(self.session_id),
                approve=self._approve_command,
                on_update=self._show_qa_report,
            )
            failed = sum(item.status == "failed" for item in report.checks)
            failed += sum(item.status == "failed" for item in report.specialists)
            self.notify(
                f"QA completed with {failed} failed check(s)." if failed else "QA completed.",
                severity="warning" if failed else "information",
            )
            self._set_activity(
                "failed" if failed else "completed",
                f"{failed} failed checks" if failed else "QA complete",
            )
        except asyncio.CancelledError:
            self.notify("QA cancelled; completed evidence was preserved.", severity="warning")
            raise
        finally:
            self.active_status = previous_status if self.active_mission_id else "Ready"
            self.request_refresh()

    def _show_qa_report(self, report: QAReport) -> None:
        previous = self._last_qa_report
        self._last_qa_report = report
        self.query_one("#qa-view", QAView).show_report(report)
        if previous is None or previous.id != report.id or previous.status != report.status:
            self._refresh_qa_history()
        self.request_refresh()

    def _refresh_qa_history(self) -> None:
        self.query_one("#qa-view", QAView).set_history(self.qa.history())

    def load_qa_report(self, report_id: str) -> None:
        report = self.qa.load(report_id)
        if report is None:
            self._refresh_qa_history()
            self.notify("That saved QA scan is unavailable or invalid.", severity="warning")
            return
        self._show_qa_report(report)
        self.notify(f"Loaded saved QA scan {report.id}.")

    @work(exclusive=True, group="review")
    async def run_review(self, mission_id: str) -> None:
        self.action_open_view("changes-view")
        self._set_activity("inspecting", "reviewing changes")
        try:
            report = await self.missions.review(
                mission_id,
                self.session_id,
                profile_override=self.providers.session_profile(self.session_id),
            )
            await self.query_one("#chat-view", ConversationView).add_message(
                report.summary,
                kind="agent" if report.approved else "error",
                role="reviewer",
                metadata=report.model_dump(mode="json"),
            )
            self.notify(
                "Independent review approved the diff."
                if report.approved
                else "Independent review found blocking issues.",
                severity="information" if report.approved else "warning",
            )
            self._set_activity(
                "completed" if report.approved else "failed",
                "review approved" if report.approved else "review found issues",
            )
        except Exception as exc:
            self._set_activity("failed", "review stopped")
            self.notify(self._actionable_error(exc), severity="error")

    @work(exclusive=True, group="index")
    async def reindex(self) -> None:
        self.active_status = "Indexing"
        self._set_activity("inspecting", "indexing repository")
        self.request_refresh()
        try:
            index = await asyncio.to_thread(self.repository.index)
            self.query_one("#repository-view", RepositoryView).refresh_data()
            self.query_one("#files-view", FilesView).refresh_data()
            self._load_references()
            self.notify(f"Indexed {len(index.files)} files")
            self._set_activity("completed", f"indexed {len(index.files)} files")
        except Exception as exc:
            self._set_activity("failed", "indexing stopped")
            self.notify(str(exc), severity="error")
        finally:
            self.active_status = "Ready"
            self.request_refresh()

    @work(exclusive=True, group="provider")
    async def provider_health(self) -> None:
        if not self.context.settings.providers:
            return
        try:
            items = await self.providers.health_all()
            self.query_one("#providers-view", ProvidersView).show_health(items)
            self.provider_health_status.update(
                {item.name: bool(item.connected) for item in items if item.connected is not None}
            )
            self.request_refresh()
        except Exception as exc:
            self.notify(f"Provider check failed: {exc}", severity="warning")

    @work(exclusive=True, group="provider")
    async def test_provider(self, name: str) -> None:
        self.action_open_view("providers-view")
        try:
            item = await self.providers.health(name)
            if item.connected is not None:
                self.provider_health_status[name] = item.connected
            self.notify(
                f"{name}: {'connected' if item.connected else item.detail}",
                severity="information" if item.connected else "error",
            )
            self.request_refresh()
            self.provider_health()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    @work(exclusive=True, group="provider-config")
    async def save_provider(self, values: dict[str, str]) -> None:
        view = self.query_one("#providers-view", ProvidersView)
        view.set_save_state("Validating provider before saving…", busy=True)
        try:
            item, models = await self.providers.configure(
                name=values["name"],
                provider_type=values["provider_type"],
                base_url=values["base_url"],
                model=values["model"],
                api_key_input=values.get("api_key_input", ""),
            )
        except Exception as exc:
            reason = str(exc)
            view.set_save_state(reason, busy=False)
            self.notify(reason, severity="error")
            return
        self.provider_health_status[item.name] = bool(item.connected)
        if models:
            view.set_openrouter_models(models, selected=item.model)
        view.provider_saved(item)
        view.clear_secret()
        view.refresh_data()
        view.set_save_state(f"Saved {item.name}. {item.detail}", busy=False)
        self.active_provider = item.name
        self.active_model = item.model
        self.request_refresh()
        self._flush_refresh()
        self.notify(
            f"{item.name} is connected. Ask anything in the chat.",
            severity="information",
        )
        self.action_open_view("chat-view")
        await self.query_one("#chat-view", ConversationView).add_message(
            f"Connected {item.name} using {item.model}.\n{item.detail}",
            kind="status",
            follow=True,
        )

    @work(exclusive=True, group="checkpoint")
    async def create_checkpoint(self, description: str) -> None:
        try:
            item = await asyncio.to_thread(
                self.checkpoints.create,
                description,
                mission_id=self.active_mission_id,
            )
            self.notify(f"Created {item.id}")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def confirm_restore(self, checkpoint_id: str) -> None:
        try:
            impact = self.checkpoints.impact(checkpoint_id)
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        files = impact["overwrite_or_create"]
        detail = "\n".join(files[:12])
        if len(files) > 12:
            detail += f"\n… and {len(files) - 12} more files"

        def resolved(result: tuple[bool, str] | None) -> None:
            if result and result[0]:
                try:
                    self.checkpoints.restore(checkpoint_id)
                    self.notify(f"Restored {checkpoint_id}")
                    self.reindex()
                except Exception as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(
            ApprovalModal(
                title="Restore Checkpoint",
                subject=f"Overwrite matching files from {checkpoint_id}",
                risk="high",
                details=detail,
            ),
            resolved,
        )

    @work(exclusive=True, group="deployment")
    async def run_deployment(self, action: str, target: str) -> None:
        self.action_open_view("deployments-view")
        view = self.query_one("#deployments-view", DeploymentsView)
        result: object
        try:
            if action == "inspect":
                result = await self.deployments.inspect(target)
            elif action == "plan":
                result = await self.deployments.plan(target)
            elif action == "verify":
                result = await self.deployments.verify(target)
            elif action in {"apply", "rollback"}:
                self._request_deployment_approval(action, target)
                return
            else:
                raise ValueError(f"Unknown deployment action {action}")
            view.show_result(result)
        except Exception as exc:
            view.show_result({"error": str(exc)})
            self.notify(self._actionable_error(exc), severity="error")

    def _request_deployment_approval(self, action: str, target: str) -> None:
        if self.interaction_mode == InteractionMode.FULL:
            self.apply_deployment(action, target)
            return

        def resolved(result: tuple[bool, str] | None) -> None:
            if result and result[0]:
                self.apply_deployment(action, target)

        self.app.push_screen(
            ApprovalModal(
                title=f"{action.title()} Deployment Approval",
                subject=f"{action.title()} target {target}",
                risk="high",
                details=(
                    "This may change running services. The deployment manager will preserve "
                    "the previous release and health-gate promotion."
                ),
            ),
            resolved,
        )

    @work(exclusive=True, group="deployment")
    async def apply_deployment(self, action: str, target: str) -> None:
        try:
            if action == "apply":
                result = await self.deployments.apply(
                    target,
                    approved=True,
                    mission_id=self.active_mission_id,
                )
            else:
                result = await self.deployments.rollback(target, approved=True)
            self.query_one("#deployments-view", DeploymentsView).show_result(result)
        except Exception as exc:
            self.notify(self._actionable_error(exc), severity="error")

    def _request_execution_approval(self, mission_id: str) -> None:
        self._show_approval(
            ApprovalRequested(
                mission_id=mission_id,
                category="mission_execution",
                subject="Execute persisted mission plan",
                risk="medium",
            )
        )

    def action_open_view(self, view_id: str) -> None:
        switcher = self.query_one("#main-workspace", ContentSwitcher)
        switcher.current = view_id
        self._highlight_navigation(view_id)
        if view_id == "chat-view":
            self.call_after_refresh(self.query_one(PromptInput).focus_prompt)
        elif view_id == "changes-view":
            self._refresh_diff()
        elif view_id == "missions-view":
            self._refresh_missions()
        elif view_id == "qa-view":
            self._refresh_qa_history()
        elif view_id == "logs-view":
            self.query_one("#logs-view", LogsView).refresh_data()
        elif view_id == "checkpoints-view":
            self.query_one("#checkpoints-view", CheckpointsView).refresh_data()
        elif view_id == "providers-view":
            self.query_one(
                "#providers-view",
                ProvidersView,
            ).ensure_openrouter_models()

    def _highlight_navigation(self, view_id: str) -> None:
        """Keep the tab bar in step with views opened by command or shortcut.

        Secondary views have no tab, so the bar clears its highlight rather than
        claiming the user is still on chat.
        """
        self.query_one("#nav-tabs", NavigationTabs).set_active(view_id)

    def on_prompt_input_chat_scroll_requested(self, event: PromptInput.ChatScrollRequested) -> None:
        conversation = self.query_one("#chat-view", ConversationView)
        if event.direction < 0:
            conversation.scroll_page_up(animate=False, force=True)
        else:
            conversation.scroll_page_down(animate=False, force=True)

    def action_command_palette(self) -> None:
        def selected(command: str | None) -> None:
            if command:
                asyncio.create_task(self.execute_command(command))

        self.app.push_screen(CommandPalette(), selected)

    def action_model_selector(self) -> None:
        def selected(profile: str | None) -> None:
            if profile:
                self.select_model(profile)

        self.app.push_screen(ModelSelector(self.providers.models()), selected)

    def select_model(self, profile: str) -> None:
        try:
            self.providers.select_for_session(self.session_id, profile)
            item = self.context.settings.models[profile]
            self.active_model = profile
            self.active_provider = item.provider
            self.request_refresh()
            self.notify(f"Using {profile} for this session; saved routing is unchanged.")
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_new_session(self) -> None:
        asyncio.create_task(self.new_session())

    def _set_interaction_mode(self, mode: InteractionMode) -> None:
        self.interaction_mode = mode
        self.missions.set_interaction_mode(self.session_id, mode)
        self.query_one("#hint-bar", VasukiHintBar).set_mode(mode.value)
        self.request_refresh()
        self.notify(f"{mode.value.title()} mode — {MODE_DESCRIPTIONS[mode]}.")

    def action_cycle_interaction_mode(self) -> None:
        index = INTERACTION_MODES.index(self.interaction_mode)
        self._set_interaction_mode(INTERACTION_MODES[(index + 1) % len(INTERACTION_MODES)])

    def action_run_tests(self) -> None:
        self.run_tests()

    def action_toggle_context(self) -> None:
        strip = self.query_one("#context-strip", ContextStrip)
        self._context_user_hidden = not strip.has_class("hidden-panel")
        strip.set_class(self._context_user_hidden, "hidden-panel")

    def action_cancel_work(self) -> None:
        workers = [
            *self.app.workers.cancel_group(self, "mission"),
            *self.app.workers.cancel_group(self, "chat"),
            *self.app.workers.cancel_group(self, "verification"),
            *self.app.workers.cancel_group(self, "qa"),
            *self.app.workers.cancel_group(self, "review"),
            *self.app.workers.cancel_group(self, "deployment"),
            *self.app.workers.cancel_group(self, "index"),
        ]
        self.query_one("#chat-view", ConversationView).finish_stream()
        if self.active_mission_id:
            try:
                details = self.missions.mission_details(self.active_mission_id)
                if details["mission"]["status"] not in {
                    MissionStatus.COMPLETED.value,
                    MissionStatus.CANCELLED.value,
                }:
                    self.missions.cancel(self.active_mission_id)
            except Exception as exc:
                self.notify(f"Could not persist cancellation: {exc}", severity="warning")
        self.active_status = "Cancelled"
        self.request_refresh()
        self.notify(
            "Cancellation requested; mission state was preserved."
            if workers
            else "No active operation."
        )

    def action_safe_quit(self) -> None:
        active = any(worker.is_running for worker in self.app.workers if worker.node is self)
        if active:
            self.notify("Cancel active work before quitting.", severity="warning")
            return
        self.app.exit()

    def on_resize(self, event: events.Resize) -> None:
        # The layout stays a single column; narrow terminals drop secondary
        # header metadata and the least-used primary tabs.
        self.set_class(event.size.width < 100, "narrow")
        self.set_class(event.size.width < 72, "very-narrow")
        self.query_one("#context-strip", ContextStrip).set_class(
            self._context_user_hidden,
            "hidden-panel",
        )

    def _actionable_error(self, exc: Exception) -> str:
        text = str(exc)
        lower = text.lower()
        actions: list[str] = []
        if not self.providers.routable_profile():
            actions.append("Connect a provider: /provider, then Validate + save")
        elif "provider" in lower or "model" in lower or "connect" in lower:
            actions.extend(
                [
                    "Check the connection with /provider <name>",
                    "Switch model with Ctrl+M",
                    "Confirm the base URL and API key in /provider",
                ]
            )
        if "docker" in lower:
            actions.extend(["Start Docker", "Switch to /runtime local if policy permits"])
        if "git" in lower or "worktree" in lower:
            actions.extend(["Inspect Git status", "Resolve workspace conflicts and retry"])
        if "step safety limit" in lower:
            actions.extend(
                [
                    "Inspect the partial changes before retrying",
                    "Use execution_mode: standard for a capable remote model",
                    "Increase the profile's max_agent_steps only for genuinely long tasks",
                ]
            )
        if not actions:
            actions.extend(["Open /logs for details", "Retry after correcting the cause"])
        return text + "\n\nPossible actions:\n- " + "\n- ".join(actions)
