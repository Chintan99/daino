"""End-to-end mission orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from daino.agents import ModelGateway, ReviewerAgent, ToolLoop, describe_incomplete_outcome
from daino.agents.tool_schemas import AGENT_TOOL_SPECS
from daino.config.models import Settings
from daino.context import ContextBuilder, ContextCompiler
from daino.events import (
    AgentRoleChanged,
    ApprovalRequested,
    CheckpointCreated,
    EventBus,
    FileChanged,
    MissionCompleted,
    MissionCreated,
    MissionFailed,
    MissionStarted,
    TaskCompleted,
    TaskStarted,
    TestsCompleted,
    TestsStarted,
    ToolCompleted,
    ToolFailed,
    ToolStarted,
)
from daino.git import GitClient
from daino.memory import MemoryManager, MemoryStore, PersistentTaskStatus
from daino.missions.evidence import EvidenceExporter
from daino.model_router import ModelRole
from daino.observability import AuditLog
from daino.persistence import Database
from daino.persistence.models import (
    Checkpoint,
    Mission,
    RequirementVersion,
    Review,
    Task,
    TaskDependency,
    ToolCall,
    VerificationRun,
)
from daino.planning import Planner, recommend_mode, validate_task_graph
from daino.repository import RepositoryIndexer
from daino.requirements import RequirementsCompiler
from daino.runtimes import DockerRuntime, LocalRuntime, Runtime
from daino.schemas import (
    AgentAction,
    ContextBundle,
    FailureReport,
    Implementation,
    MissionStatus,
    ProjectMode,
    RequirementSpec,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolResult,
    VerificationReport,
)
from daino.security import PolicyEngine
from daino.security.commands import CommandGate
from daino.tools import ActionExecutor, EditTools
from daino.tools.commands import CommandRunner
from daino.utils.ids import new_id
from daino.verification import RepairLoop, VerificationEngine
from daino.workspace import Workspace, WorkspaceManager

MAX_REVIEW_REPAIR_ATTEMPTS = 2


class MissionService:
    """Runs sequential, verification-gated tasks in isolated Git worktrees."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        database: Database,
        events: EventBus | None = None,
        memory: MemoryManager | None = None,
    ) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.database = database
        self.log = AuditLog(self.root)
        self.workspace_manager = WorkspaceManager(self.root)
        self.events = events or EventBus()
        self.gateway = ModelGateway(settings, database, self.events)
        self.memory = memory or MemoryManager(database, self.root, settings)

    def _gateway(self, profile_override: str = "") -> ModelGateway:
        """Return the gateway for agents, pinned when a session model was chosen."""
        if not profile_override:
            return self.gateway
        return self.gateway.with_profile(profile_override)

    def _role_available(self, role: ModelRole, profile_override: str = "") -> bool:
        """Report whether a role can run, honouring an explicitly pinned profile."""
        profile_name = profile_override or self.settings.routing.get(role.value)
        if not profile_name or profile_name not in self.settings.models:
            return False
        return self.settings.models[profile_name].provider in self.settings.providers

    def create(
        self,
        request: str,
        mode: ProjectMode | None = None,
        *,
        start_task: bool = True,
    ) -> Mission:
        """Open a mission. ``start_task=False`` leaves working memory to the caller.

        A chat turn opens a mission per turn but must continue the session's
        existing working memory, so it opts out of the fresh task this would
        otherwise create.
        """
        mission = Mission(
            id=new_id("mission"),
            project_id=self.database.project().id,
            request=request,
            mode=(mode or recommend_mode(request)).value,
            status=MissionStatus.CREATED.value,
        )
        with self.database.session() as session:
            session.add(mission)
        self.log.emit("mission.created", mission_id=mission.id, mode=mission.mode)
        self.events.publish(
            MissionCreated(
                mission_id=mission.id,
                request=mission.request,
                mode=mission.mode,
            )
        )
        if start_task and self.settings.memory.enabled and self.settings.memory.auto_save:
            self.memory.start_task(
                request,
                mission_id=mission.id,
                status=PersistentTaskStatus.PENDING,
            )
        return mission

    async def plan(
        self,
        request: str,
        mode: ProjectMode | None = None,
        *,
        supplemental_context: str = "",
        profile_override: str = "",
    ) -> tuple[Mission, RequirementSpec, TaskPlan]:
        mission = self.create(request, mode)
        return await self._plan_existing(
            mission,
            supplemental_context=supplemental_context,
            profile_override=profile_override,
        )

    async def _plan_existing(
        self,
        mission: Mission,
        *,
        supplemental_context: str = "",
        profile_override: str = "",
    ) -> tuple[Mission, RequirementSpec, TaskPlan]:
        # Checked before any model call: a coding mission needs a worktree, so
        # planning one in a plain directory only bills the user for a plan that
        # cannot be executed.
        if not self.workspace_manager.git.ensure_repository():
            raise RuntimeError(
                "Coding missions need Git, and it is not usable here. Install Git "
                f"or initialize {self.root} by hand, then retry. Plain questions "
                "work without one."
            )
        self._update_mission(mission.id, status=MissionStatus.PLANNING.value)
        state = self.memory.task_for_mission(mission.id)
        if state:
            self.memory.update_task(state.task_id, status=PersistentTaskStatus.PLANNING)
        gateway = self._gateway(profile_override)
        indexer = RepositoryIndexer(self.root)
        summary = indexer.summary()
        persistent = self.memory.task_for_mission(mission.id)
        memory_context = ContextBuilder(
            self.root,
            self.settings,
            self.memory,
            indexer=indexer,
        ).build_question_context(
            mission.request,
            task_state_id=persistent.task_id if persistent else None,
        )
        if memory_context:
            summary += f"\n\n{memory_context}"
        if supplemental_context:
            summary += f"\n\nExplicit user context:\n{supplemental_context}"
        architect_gateway = (
            gateway if self._role_available(ModelRole.ARCHITECT, profile_override) else None
        )
        compiler = RequirementsCompiler(architect_gateway)
        requirements = await compiler.compile(mission.id, mission.request, summary)
        with self.database.session() as session:
            session.add(
                RequirementVersion(
                    id=new_id("requirements"),
                    mission_id=mission.id,
                    version=1,
                    content=requirements.model_dump(mode="json"),
                    approved=True,
                )
            )
        planner_gateway = (
            gateway if self._role_available(ModelRole.PLANNER, profile_override) else None
        )
        task_plan = await Planner(planner_gateway).plan(
            mission.id, requirements, summary, ProjectMode(mission.mode)
        )
        ordered = validate_task_graph(task_plan)
        id_map = {task.id: new_id("task") for task in ordered}
        normalized: list[TaskSpec] = []
        with self.database.session() as session:
            for spec in ordered:
                mapped = spec.model_copy(
                    update={
                        "id": id_map[spec.id],
                        "dependencies": [id_map[item] for item in spec.dependencies],
                        "status": TaskStatus.PENDING,
                    }
                )
                normalized.append(mapped)
                session.add(
                    Task(
                        id=mapped.id,
                        mission_id=mission.id,
                        title=mapped.title,
                        objective=mapped.objective,
                        status=TaskStatus.PENDING.value,
                        risk_level=mapped.risk_level,
                        specification=mapped.model_dump(mode="json"),
                        assigned_model=mapped.assigned_model,
                    )
                )
            for spec in normalized:
                for dependency in spec.dependencies:
                    session.add(TaskDependency(task_id=spec.id, depends_on_id=dependency))
        result = task_plan.model_copy(update={"tasks": normalized})
        if state:
            serialized_plan = [
                {
                    "id": item.id,
                    "content": item.title,
                    "objective": item.objective,
                    "status": item.status.value,
                }
                for item in normalized
            ]
            self.memory.update_task(
                state.task_id,
                interpreted_goal=requirements.problem_statement,
                plan=serialized_plan,
                pending_steps=[item.title for item in normalized],
                current_step=normalized[0].title if normalized else "",
                status=PersistentTaskStatus.PENDING,
            )
        self._update_mission(mission.id, status=MissionStatus.AWAITING_APPROVAL.value)
        self.log.emit("mission.planned", mission_id=mission.id, tasks=len(normalized))
        self.events.publish(
            ApprovalRequested(
                mission_id=mission.id,
                category="mission_execution",
                subject="Approve the implementation plan",
                risk=max((task.risk_level for task in normalized), default="medium"),
            )
        )
        return self.get(mission.id), requirements, result

    def _runtime(self, workspace: Path) -> Runtime:
        policy = PolicyEngine(self.settings.security)
        if self.settings.runtime.default == "docker":
            return DockerRuntime(
                workspace,
                policy=policy,
                image=self.settings.runtime.docker_image,
                cpu_limit=self.settings.runtime.cpu_limit,
                memory_limit=self.settings.runtime.memory_limit,
                network_access=self.settings.runtime.network_access == "allowed",
                timeout=self.settings.runtime.command_timeout_seconds,
            )
        if self.settings.runtime.default == "local":
            return LocalRuntime(
                workspace,
                policy=policy,
                timeout=self.settings.runtime.command_timeout_seconds,
            )
        raise RuntimeError("SSH runtime cannot be used for coding missions")

    async def run(
        self,
        request: str,
        mode: ProjectMode | None = None,
        *,
        profile_override: str = "",
    ) -> tuple[Mission, Path | None]:
        mission, requirements, plan = await self.plan(
            request,
            mode,
            profile_override=profile_override,
        )
        return await self.execute(
            mission.id,
            requirements,
            plan,
            profile_override=profile_override,
        )

    async def execute(
        self,
        mission_id: str,
        requirements: RequirementSpec | None = None,
        plan: TaskPlan | None = None,
        *,
        require_change_approval: bool = False,
        profile_override: str = "",
        resume: bool = False,
    ) -> tuple[Mission, Path | None]:
        mission = self.get(mission_id)
        if mission.status == MissionStatus.RUNNING.value and not resume:
            raise RuntimeError(f"Mission {mission_id} is already running")
        if not self.settings.git.use_worktrees:
            with self.database.session() as session:
                conflicting = session.scalar(
                    select(Mission).where(
                        Mission.project_id == mission.project_id,
                        Mission.id != mission_id,
                        Mission.status.in_(
                            [
                                MissionStatus.RUNNING.value,
                                MissionStatus.VERIFYING.value,
                                MissionStatus.REVIEWING.value,
                            ]
                        ),
                    )
                )
            if conflicting is not None:
                raise RuntimeError(f"Mission {conflicting.id} already owns the project worktree")
        if not self._role_available(ModelRole.BUILDER, profile_override):
            raise RuntimeError(
                "No builder model configured. Add a provider and route the builder role."
            )
        if requirements is None or plan is None:
            requirements, plan = self._load_plan(mission_id)
        resumable_workspace = (
            resume
            and mission.workspace_path is not None
            and Path(mission.workspace_path).is_dir()
            and mission.branch is not None
            and mission.initial_revision is not None
        )
        workspace = (
            Workspace(
                mission.id,
                Path(mission.workspace_path or self.root),
                mission.branch or "",
                mission.initial_revision or "HEAD",
                "",
            )
            if resumable_workspace
            else self.workspace_manager.create(
                mission.id,
                mission.request,
                use_worktree=self.settings.git.use_worktrees,
            )
        )
        self._update_mission(
            mission.id,
            status=MissionStatus.RUNNING.value,
            workspace_path=str(workspace.path),
            branch=workspace.branch,
            initial_revision=workspace.initial_revision,
        )
        persistent = self.memory.task_for_mission(mission.id)
        if persistent:
            self.memory.update_task(
                persistent.task_id,
                status=PersistentTaskStatus.IN_PROGRESS,
                repository=workspace.path.as_posix(),
                branch=workspace.branch,
            )
        self.events.publish(
            MissionStarted(
                mission_id=mission.id,
                workspace=str(workspace.path),
                branch=workspace.branch,
            )
        )
        if resumable_workspace:
            self.log.emit("task_resumed", mission_id=mission.id, workspace=str(workspace.path))
        else:
            checkpoint_id, checkpoint_path = self.workspace_manager.checkpoint(
                workspace, "Before mission changes", mission_id=mission.id
            )
            with self.database.session() as session:
                session.add(
                    Checkpoint(
                        id=checkpoint_id,
                        mission_id=mission.id,
                        revision=workspace.initial_revision,
                        archive_path=str(checkpoint_path),
                        description="Before mission changes",
                    )
                )
            self.events.publish(
                CheckpointCreated(
                    mission_id=mission.id,
                    checkpoint_id=checkpoint_id,
                    description="Before mission changes",
                )
            )
        try:
            await self._execute_tasks(
                workspace,
                requirements,
                plan,
                commit_verified=not require_change_approval,
                profile_override=profile_override,
            )
            evidence_path = await self._review_and_finish(
                workspace,
                requirements,
                plan,
                require_change_approval=require_change_approval,
                profile_override=profile_override,
            )
            return self.get(mission.id), evidence_path
        except Exception as exc:
            current = self.get(mission.id)
            if current.status != MissionStatus.BLOCKED.value:
                self._update_mission(
                    mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
                )
            self.log.emit("mission.failed", mission_id=mission.id, error=str(exc))
            self.events.publish(MissionFailed(mission_id=mission.id, error=str(exc)))
            persistent = self.memory.task_for_mission(mission.id)
            if persistent:
                self.memory.update_task(
                    persistent.task_id,
                    status=(
                        PersistentTaskStatus.BLOCKED
                        if current.status == MissionStatus.BLOCKED.value
                        else PersistentTaskStatus.FAILED
                    ),
                    errors=[*persistent.errors, str(exc)],
                    unresolved_problems=[*persistent.unresolved_problems, str(exc)],
                    last_action="mission_failed",
                )
            raise

    async def resume(
        self,
        mission_id: str,
        *,
        require_change_approval: bool = False,
        profile_override: str = "",
    ) -> tuple[Mission, Path | None]:
        """Continue a persisted mission after a process or provider interruption."""
        mission = self.get(mission_id)
        allowed = {
            MissionStatus.AWAITING_APPROVAL.value,
            MissionStatus.RUNNING.value,
            MissionStatus.VERIFYING.value,
            MissionStatus.REVIEWING.value,
            MissionStatus.FAILED.value,
            MissionStatus.BLOCKED.value,
        }
        if mission.status not in allowed:
            raise RuntimeError(f"Mission {mission_id} is not resumable from {mission.status}")
        return await self.execute(
            mission_id,
            require_change_approval=require_change_approval,
            profile_override=profile_override,
            resume=True,
        )

    async def _execute_tasks(
        self,
        workspace: Workspace,
        requirements: RequirementSpec,
        plan: TaskPlan,
        *,
        commit_verified: bool = True,
        profile_override: str = "",
    ) -> None:
        runtime = self._runtime(workspace.path)
        await runtime.prepare()
        indexer = RepositoryIndexer(workspace.path)
        indexer.build()
        gateway = self._gateway(profile_override)
        budgeter = getattr(gateway, "context_budget", None)
        model_budget = (
            budgeter(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(budgeter)
            else self.settings.project.context_budget_tokens
        )
        profile_resolver = getattr(gateway, "execution_profile", None)
        builder_profile = (
            profile_resolver(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(profile_resolver)
            else None
        )
        context_reserve = min(2_048, max(512, model_budget // 4))
        compiler = ContextCompiler(
            workspace.path,
            indexer,
            min(
                self.settings.project.context_budget_tokens,
                max(512, model_budget - context_reserve),
            ),
        )
        command_runner = CommandRunner(
            runtime,
            CommandGate(self.settings.security),
            runtime_name=self.settings.runtime.default,
            default_timeout=self.settings.runtime.command_timeout_seconds,
        )
        memory = MemoryStore(self.database)
        persistent = self.memory.task_for_mission(workspace.mission_id)
        context_builder = ContextBuilder(
            workspace.path,
            self.settings,
            self.memory,
            indexer=indexer,
            token_budget=compiler.token_budget,
        )
        with self.database.session() as session:
            completed = set(
                session.scalars(
                    select(Task.id).where(
                        Task.mission_id == workspace.mission_id,
                        Task.status == TaskStatus.COMPLETED.value,
                    )
                ).all()
            )
        try:
            for spec in validate_task_graph(plan):
                if spec.id in completed:
                    continue
                if not set(spec.dependencies) <= completed:
                    raise RuntimeError(f"Dependencies not completed for {spec.id}")
                self._update_task(spec.id, TaskStatus.RUNNING)
                self.events.publish(
                    TaskStarted(
                        mission_id=workspace.mission_id,
                        task_id=spec.id,
                        title=spec.title,
                    )
                )
                decisions = memory.relevant_decisions([*spec.expected_files, *spec.allowed_files])
                context = context_builder.build(
                    spec,
                    current_user_instruction=spec.objective,
                    task_state_id=persistent.task_id if persistent else None,
                    execution_profile=builder_profile,
                )
                if decisions:
                    packet = context.task_packet
                    context = context.model_copy(
                        update={
                            "architecture_decisions": [
                                *context.architecture_decisions,
                                *decisions,
                            ],
                            "task_packet": (
                                packet.model_copy(
                                    update={
                                        "active_decisions": [
                                            *packet.active_decisions,
                                            *decisions,
                                        ][:6]
                                    }
                                )
                                if packet
                                else None
                            ),
                        }
                    )
                if persistent:
                    pending = [
                        item.title
                        for item in validate_task_graph(plan)
                        if item.id not in completed and item.id != spec.id
                    ]
                    self.memory.update_task(
                        persistent.task_id,
                        current_step=spec.title,
                        pending_steps=pending,
                        status=PersistentTaskStatus.IN_PROGRESS,
                    )
                # The agent was shown the compiled file contents, so they count
                # as already read: edits to those files may land immediately,
                # while any other existing file must be read first or the gate
                # rejects a blind overwrite.
                editor = EditTools(
                    workspace.path,
                    spec.allowed_files,
                    require_read_before_write=True,
                    seen_files=set(context.included_paths),
                )
                executor = ActionExecutor(
                    editor,
                    command_runner,
                    memory=self.memory,
                    memory_task_id=persistent.task_id if persistent else None,
                )
                implementation, changed = await self._run_builder(
                    workspace,
                    spec,
                    context,
                    gateway,
                    executor,
                    debugger=False,
                    attempts=0,
                )
                indexer.build()
                self._update_task(spec.id, TaskStatus.VERIFYING)
                engine = VerificationEngine(workspace.path, runtime)
                # The approved task contract is authoritative. A builder may
                # suggest useful checks in ``finish``, but letting those replace
                # the planner's commands allows a malformed ad-hoc one-liner to
                # sink correct code after the planned check already existed.
                commands = spec.verification_commands or implementation.verification_commands
                if not commands:
                    commands = engine.discover_commands()
                self.events.publish(
                    TestsStarted(mission_id=workspace.mission_id, commands=commands)
                )
                loop = RepairLoop(
                    engine,
                    local_attempts=self.settings.verification.repair_attempts_local,
                    total_attempts=self.settings.verification.total_attempts,
                )

                async def repair(
                    failure: FailureReport,
                    attempt: int,
                    escalated: bool,
                    current_spec: TaskSpec = spec,
                    current_editor: EditTools = editor,
                    current_decisions: list[str] = decisions,
                ) -> bool:
                    self.events.publish(
                        AgentRoleChanged(
                            mission_id=workspace.mission_id,
                            role="debugger" if escalated else "builder",
                            details={"repair_attempt": attempt},
                        )
                    )
                    repair_role = ModelRole.DEBUGGER if escalated else ModelRole.BUILDER
                    repair_profile = (
                        profile_resolver(
                            repair_role,
                            tools=AGENT_TOOL_SPECS,
                        )
                        if callable(profile_resolver)
                        else None
                    )
                    refreshed = context_builder.build(
                        current_spec,
                        failure_summary=failure.model_dump_json(indent=2),
                        current_user_instruction=current_spec.objective,
                        task_state_id=persistent.task_id if persistent else None,
                        execution_profile=repair_profile,
                    )
                    if current_decisions:
                        refreshed = refreshed.model_copy(
                            update={
                                "architecture_decisions": [
                                    *refreshed.architecture_decisions,
                                    *current_decisions,
                                ]
                            }
                        )
                    # The refreshed context shows the failing files, so they count
                    # as read; without this the read-before-write gate rejects the
                    # debugger's first edit of a file the builder created.
                    for shown in refreshed.included_paths:
                        current_editor.mark_seen(shown)
                    _, repair_changed = await self._run_builder(
                        workspace,
                        current_spec,
                        refreshed,
                        gateway,
                        ActionExecutor(
                            current_editor,
                            command_runner,
                            memory=self.memory,
                            memory_task_id=persistent.task_id if persistent else None,
                        ),
                        debugger=escalated,
                        attempts=attempt,
                    )
                    if repair_changed:
                        indexer.build()
                    return bool(repair_changed)

                def observe_report(
                    typed_report: VerificationReport,
                    attempt: int,
                ) -> None:
                    duration = (typed_report.finished_at - typed_report.started_at).total_seconds()
                    self.events.publish(
                        TestsCompleted(
                            mission_id=workspace.mission_id,
                            passed=typed_report.passed,
                            passed_count=sum(check.passed for check in typed_report.checks),
                            failed_count=len(typed_report.failures),
                            duration_seconds=duration,
                            failures=[
                                item.model_dump(mode="json") for item in typed_report.failures
                            ],
                            details={"repair_attempt": attempt},
                        )
                    )

                report, attempts = await loop.run(
                    commands,
                    repair,
                    observe=observe_report,
                )
                with self.database.session() as session:
                    session.add(
                        VerificationRun(
                            id=new_id("verification"),
                            mission_id=workspace.mission_id,
                            task_id=spec.id,
                            passed=report.passed,
                            report=report.model_dump(mode="json"),
                        )
                    )
                    for check in report.checks:
                        session.add(
                            ToolCall(
                                id=new_id("tool-call"),
                                mission_id=workspace.mission_id,
                                tool="verification.command",
                                arguments={"command": check.command},
                                result_summary=(
                                    check.skip_reason
                                    if check.skipped
                                    else "passed"
                                    if check.passed or check.result is None
                                    else check.result.stderr[-1000:] or check.result.stdout[-1000:]
                                ),
                                duration_seconds=(
                                    check.result.duration_seconds if check.result else 0.0
                                ),
                                success=check.passed,
                            )
                        )
                if persistent:
                    self.memory.update_task(
                        persistent.task_id,
                        test_status=report.model_dump(mode="json"),
                    )
                if not report.passed:
                    self._update_task(
                        spec.id,
                        TaskStatus.FAILED,
                        attempt_count=attempts,
                        evidence=[item.model_dump(mode="json") for item in report.failures],
                    )
                    raise RuntimeError(
                        f"Verification failed for {spec.title}: {report.failures[0].summary}"
                    )
                revision = None
                if commit_verified and self.settings.git.auto_commit_verified_tasks:
                    changed_paths = sorted(set(changed))
                    revision = (
                        GitClient(workspace.path).commit(
                            f"{spec.title}\n\nDaino-Mission: {workspace.mission_id}",
                            paths=changed_paths,
                        )
                        if changed_paths
                        else GitClient(workspace.path).revision()
                    )
                self._update_task(
                    spec.id,
                    TaskStatus.COMPLETED,
                    attempt_count=attempts,
                    evidence=[
                        {"verification": report.model_dump(mode="json")},
                        {"commit": revision},
                        {"files": sorted(set(changed))},
                    ],
                )
                self.events.publish(
                    TaskCompleted(
                        mission_id=workspace.mission_id,
                        task_id=spec.id,
                        title=spec.title,
                    )
                )
                completed.add(spec.id)
                if persistent:
                    refreshed = self.memory.load_task(persistent.task_id)
                    self.memory.update_task(
                        persistent.task_id,
                        completed_steps=_append_unique(refreshed.completed_steps, spec.title),
                        current_step="",
                    )
        finally:
            await runtime.cleanup()

    async def _run_builder(
        self,
        workspace: Workspace,
        spec: TaskSpec,
        context: ContextBundle,
        gateway: ModelGateway,
        executor: ActionExecutor,
        *,
        debugger: bool,
        attempts: int,
    ) -> tuple[Implementation, list[str]]:
        """Drive the iterative tool loop and audit every action it executes."""
        role = ModelRole.DEBUGGER if debugger else ModelRole.BUILDER

        def started(action: AgentAction) -> None:
            subject = (
                action.path
                or action.query
                or action.command
                or action.url
                or action.pattern
                or action.summary
                or action.action.replace("_", " ")
            )
            self.events.publish(
                ToolStarted(
                    mission_id=workspace.mission_id,
                    tool=f"agent.{action.action}",
                    summary=subject,
                    details={"task_id": spec.id, "role": role.value},
                )
            )

        def observe(action: AgentAction, result: ToolResult, paths: list[str]) -> None:
            subject = (
                action.path
                or action.query
                or action.command
                or action.url
                or action.pattern
                or action.summary
                or action.action.replace("_", " ")
            )
            tool_name = f"agent.{action.action}"
            if result.success:
                self.events.publish(
                    ToolCompleted(
                        mission_id=workspace.mission_id,
                        tool=tool_name,
                        summary=subject,
                        duration_seconds=result.duration_seconds,
                        details={"task_id": spec.id, "role": role.value},
                    )
                )
            else:
                self.events.publish(
                    ToolFailed(
                        mission_id=workspace.mission_id,
                        tool=tool_name,
                        error=result.error or "action failed",
                        details={"task_id": spec.id, "role": role.value},
                    )
                )
            for path in paths:
                self.events.publish(
                    FileChanged(
                        mission_id=workspace.mission_id,
                        path=path,
                        action=action.action,
                        details={"task_id": spec.id, "role": role.value},
                    )
                )
            with self.database.session() as session:
                session.add(
                    ToolCall(
                        id=new_id("tool-call"),
                        mission_id=workspace.mission_id,
                        tool=tool_name,
                        arguments=action.model_dump(mode="json"),
                        result_summary=(result.error or _action_summary(result))[:1000],
                        duration_seconds=result.duration_seconds,
                        success=result.success,
                    )
                )
            persistent = self.memory.task_for_mission(workspace.mission_id)
            if persistent:
                observed_paths = paths or ([action.path] if action.path else [])
                output = result.error or _action_summary(result)
                updated = self.memory.record_action(
                    persistent.task_id,
                    action=action.action,
                    paths=observed_paths,
                    command=action.command if action.action == "run_command" else "",
                    success=result.success,
                    output=output,
                    error=result.error or "",
                )
                if action.action == "resolve_command_failure" and result.success and updated.errors:
                    self.memory.remember_failure(
                        updated.errors[-1],
                        cause="Environment-specific command failure",
                        solution=f"Equivalent check passed: {action.evidence_command}",
                        context=updated.interpreted_goal or updated.original_request,
                        failed_attempts=[action.command],
                        task_id=updated.task_id,
                    )
                if action.action == "todo" and result.success:
                    todos = [item.model_dump(mode="json") for item in action.todos]
                    self.memory.update_task(
                        persistent.task_id,
                        plan=todos,
                        completed_steps=[
                            item.content for item in action.todos if item.status == "completed"
                        ],
                        pending_steps=[
                            item.content for item in action.todos if item.status == "pending"
                        ],
                        current_step=next(
                            (item.content for item in action.todos if item.status == "in_progress"),
                            "",
                        ),
                    )

        outcome = await ToolLoop(
            gateway,
            role,
            executor,
            debugger=debugger,
            attempts=attempts,
            on_action_start=started,
        ).run(workspace.mission_id, context, on_action=observe)
        if not outcome.completed:
            raise RuntimeError(
                describe_incomplete_outcome(
                    outcome,
                    role_label=role.value,
                    pinned=bool(getattr(gateway, "profile_override", "")),
                )
            )
        return outcome.implementation, outcome.changed

    async def _review_and_finish(
        self,
        workspace: Workspace,
        requirements: RequirementSpec,
        plan: TaskPlan,
        *,
        require_change_approval: bool = False,
        profile_override: str = "",
    ) -> Path | None:
        self._update_mission(workspace.mission_id, status=MissionStatus.REVIEWING.value)
        git = GitClient(workspace.path)
        if self.settings.verification.require_review:
            if not self._role_available(ModelRole.REVIEWER, profile_override):
                raise RuntimeError(
                    "Independent review is required but no reviewer route is configured"
                )
            for review_attempt in range(MAX_REVIEW_REPAIR_ATTEMPTS + 1):
                diff = git.diff(workspace.initial_revision)
                with self.database.session() as session:
                    verifications = session.scalars(
                        select(VerificationRun).where(
                            VerificationRun.mission_id == workspace.mission_id
                        )
                    ).all()
                    verification_json = json.dumps(
                        [item.report for item in verifications], indent=2, default=str
                    )
                review = await ReviewerAgent(self._gateway(profile_override)).review(
                    workspace.mission_id,
                    requirements,
                    [criterion for task in plan.tasks for criterion in task.acceptance_criteria],
                    diff,
                    verification_json,
                )
                with self.database.session() as session:
                    session.add(
                        Review(
                            id=new_id("review"),
                            mission_id=workspace.mission_id,
                            approved=review.approved,
                            report=review.model_dump(mode="json"),
                        )
                    )
                if review.approved:
                    break
                if review_attempt >= MAX_REVIEW_REPAIR_ATTEMPTS:
                    self._update_mission(
                        workspace.mission_id,
                        status=MissionStatus.BLOCKED.value,
                        failure=review.summary,
                    )
                    raise RuntimeError(
                        f"Independent review rejected the mission after "
                        f"{MAX_REVIEW_REPAIR_ATTEMPTS} repair attempts: {review.summary}"
                    )
                await self._repair_review_findings(
                    workspace,
                    requirements,
                    plan,
                    review.model_dump_json(indent=2),
                    finding_files=[item.file for item in review.findings if item.file],
                    missing_tests=review.missing_tests,
                    commit_verified=not require_change_approval,
                    profile_override=profile_override,
                )
                self._update_mission(
                    workspace.mission_id,
                    status=MissionStatus.REVIEWING.value,
                )
        if require_change_approval:
            self._update_mission(
                workspace.mission_id,
                status=MissionStatus.AWAITING_CHANGE_APPROVAL.value,
            )
            self.events.publish(
                ApprovalRequested(
                    mission_id=workspace.mission_id,
                    category="mission_changes",
                    subject="Approve reviewed mission changes and create the final commit",
                    risk="medium",
                    details={
                        "files_changed": git.run(
                            "diff",
                            "--name-only",
                            workspace.initial_revision,
                        ).stdout.splitlines()
                    },
                )
            )
            return None
        return self.finalize_changes(workspace.mission_id)

    async def _repair_review_findings(
        self,
        workspace: Workspace,
        requirements: RequirementSpec,
        original_plan: TaskPlan,
        review_json: str,
        *,
        finding_files: list[str],
        missing_tests: list[str],
        commit_verified: bool,
        profile_override: str,
    ) -> None:
        """Turn a rejected independent review into one bounded corrective task."""
        original_scopes = [
            path
            for task in original_plan.tasks
            for path in [*task.expected_files, *task.allowed_files]
        ]
        test_scopes = [path for path in original_scopes if "test" in Path(path).name.lower()]
        allowed = list(dict.fromkeys([*finding_files, *test_scopes]))
        if missing_tests and not test_scopes:
            allowed.append("tests/**")
        if not allowed:
            allowed = list(dict.fromkeys(original_scopes))
        if not allowed:
            raise RuntimeError("Independent review rejected the mission without a repairable scope")

        commands = list(
            dict.fromkeys(
                self.settings.verification.commands
                or [
                    command
                    for task in original_plan.tasks
                    for command in task.verification_commands
                ]
            )
        )
        repair = TaskSpec(
            id=new_id("task"),
            title="Address independent review findings",
            objective=(
                "Correct the independent review findings below without expanding the requested "
                f"scope. Re-run the supplied verification commands.\n\n{review_json}"
            ),
            risk_level="medium",
            expected_files=list(dict.fromkeys(finding_files)),
            allowed_files=allowed,
            acceptance_criteria=[
                "Every evidence-backed review finding is corrected.",
                *missing_tests,
            ],
            verification_commands=commands,
        )
        with self.database.session() as session:
            session.add(
                Task(
                    id=repair.id,
                    mission_id=workspace.mission_id,
                    title=repair.title,
                    objective=repair.objective,
                    status=TaskStatus.PENDING.value,
                    risk_level=repair.risk_level,
                    specification=repair.model_dump(mode="json"),
                    assigned_model=repair.assigned_model,
                )
            )
        await self._execute_tasks(
            workspace,
            requirements,
            TaskPlan(
                summary="Repair independent review findings",
                mode=ProjectMode(self.get(workspace.mission_id).mode),
                tasks=[repair],
            ),
            commit_verified=commit_verified,
            profile_override=profile_override,
        )

    def finalize_changes(self, mission_id: str) -> Path:
        """Commit approved changes and export the final evidence bundle."""
        mission = self.get(mission_id)
        if not mission.workspace_path:
            raise RuntimeError("Mission does not have a workspace")
        if mission.status not in {
            MissionStatus.REVIEWING.value,
            MissionStatus.AWAITING_CHANGE_APPROVAL.value,
        }:
            raise RuntimeError(f"Mission {mission_id} is not awaiting change finalization")
        git = GitClient(Path(mission.workspace_path))
        with self.database.session() as session:
            mutations = session.scalars(
                select(ToolCall).where(
                    ToolCall.mission_id == mission_id,
                    ToolCall.success.is_(True),
                    ToolCall.tool.in_(
                        ["agent.write", "agent.replace", "agent.multi_edit", "agent.delete"]
                    ),
                )
            ).all()
        changed_paths = sorted(
            {
                str(call.arguments.get("path", "")).strip().removeprefix("./")
                for call in mutations
                if call.arguments.get("path")
            }
        )
        final_revision = (
            git.commit(
                f"{mission.request[:72]}\n\nDaino-Mission: {mission_id}",
                paths=changed_paths,
            )
            if changed_paths
            else git.revision()
        )
        self._update_mission(
            mission_id,
            status=MissionStatus.COMPLETED.value,
            final_revision=final_revision,
        )
        persistent = self.memory.task_for_mission(mission_id)
        if persistent:
            self.memory.extract(
                mission.request,
                task_id=persistent.task_id,
                session_id=persistent.session_id,
                source="user request",
                source_type="user",
            )
            self.memory.complete_task(
                persistent.task_id,
                summary=f"Completed mission: {mission.request}",
                outcome="completed",
            )
        path = EvidenceExporter(self.root, self.database).export(mission_id, "markdown")
        self.log.emit(
            "mission.completed",
            mission_id=mission_id,
            commit=final_revision,
            evidence=str(path),
        )
        self.events.publish(
            MissionCompleted(
                mission_id=mission_id,
                evidence_path=str(path),
            )
        )
        return path

    def _load_plan(self, mission_id: str) -> tuple[RequirementSpec, TaskPlan]:
        with self.database.session() as session:
            requirement = session.scalar(
                select(RequirementVersion)
                .where(RequirementVersion.mission_id == mission_id)
                .order_by(RequirementVersion.version.desc())
            )
            tasks = session.scalars(
                select(Task).where(Task.mission_id == mission_id).order_by(Task.created_at)
            ).all()
            mission = session.get(Mission, mission_id)
            if requirement is None or mission is None or not tasks:
                raise RuntimeError("Mission does not have a persisted plan")
            specs = [TaskSpec.model_validate(task.specification) for task in tasks]
            return (
                RequirementSpec.model_validate(requirement.content),
                TaskPlan(
                    summary=mission.request,
                    mode=ProjectMode(mission.mode),
                    tasks=specs,
                ),
            )

    def get(self, mission_id: str) -> Mission:
        with self.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"Unknown mission {mission_id}")
            session.expunge(mission)
            return mission

    def _update_mission(self, mission_id: str, **fields: object) -> None:
        with self.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None:
                raise ValueError(mission_id)
            for name, value in fields.items():
                setattr(mission, name, value)

    def _update_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        attempt_count: int | None = None,
        evidence: list[object] | None = None,
    ) -> None:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(task_id)
            task.status = status.value
            if attempt_count is not None:
                task.attempt_count = attempt_count
            if evidence is not None:
                task.evidence = evidence


def _action_summary(result: ToolResult) -> str:
    data = result.data or {}
    if not data:
        return "ok"
    parts = [f"{key}: {value}" for key, value in data.items() if key != "content"]
    return "; ".join(parts) or "ok"


def _append_unique(values: list[str], value: str) -> list[str]:
    return list(dict.fromkeys([*values, value]))
