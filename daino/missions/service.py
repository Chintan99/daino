"""End-to-end mission orchestration."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select

from daino.agents import (
    THRASHING_COMPACTIONS,
    IncompleteRun,
    ModelGateway,
    ReviewerAgent,
    ToolLoop,
    describe_incomplete_outcome,
)
from daino.agents.tool_schemas import AGENT_TOOL_SPECS
from daino.config.models import Settings
from daino.context import CapabilityEnvelope, ContextBuilder, ContextCompiler
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
    TaskSplit,
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
from daino.planning.planner import outline_of
from daino.planning.sizing import measure_scope, split_task
from daino.repository import RepositoryIndexer
from daino.requirements import RequirementsCompiler
from daino.runtimes import DockerRuntime, LocalRuntime, Runtime, SandboxedLocalRuntime
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
    VerificationCheck,
    VerificationReport,
)
from daino.security import PolicyEngine
from daino.security.commands import CommandGate
from daino.tools import ActionExecutor, EditTools
from daino.tools.commands import CommandRunner
from daino.utils.ids import new_id
from daino.verification import RepairLoop, VerificationEngine
from daino.verification.engine import missing_executable
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
        # The tasks are sized for the model that will *execute* them, which the
        # shipped configuration routes to a different (usually smaller) profile
        # than the planner. Guarded twice: the fake gateways in the test suite
        # expose only `structured`, and planning is deliberately allowed to
        # succeed with no builder routed at all — that is enforced later, when a
        # build is actually attempted.
        envelope_resolver = getattr(gateway, "capability_envelope", None)
        envelope = (
            envelope_resolver(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(envelope_resolver)
            and self._role_available(ModelRole.BUILDER, profile_override)
            else None
        )
        task_plan = await Planner(planner_gateway).plan(
            mission.id, requirements, summary, ProjectMode(mission.mode), envelope=envelope
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
                        # `TaskSpec` is a StrictModel, so the planner is able to
                        # emit this — and a planned task that claimed to be a
                        # slice of something would have its commit deferred
                        # waiting for siblings that do not exist.
                        "slice_of": "",
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
        if self.settings.runtime.default == "sandbox":
            return SandboxedLocalRuntime(
                workspace,
                policy=policy,
                timeout=self.settings.runtime.command_timeout_seconds,
                network_access=self.settings.runtime.network_access == "allowed",
                passthrough_env=frozenset(self.settings.runtime.sandbox_passthrough_env),
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
            await self._run_integration_gate(workspace)
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
        envelope_resolver = getattr(gateway, "capability_envelope", None)
        builder_envelope = (
            envelope_resolver(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(envelope_resolver)
            else None
        )
        # Sizes in bytes, straight from the index the planner was shown. A task
        # is measured against the same numbers the planner was given, so a task
        # that overruns here is one the planner got wrong rather than one this
        # code and the planner disagree about.
        index = indexer.load()
        file_sizes = {item.path: item.size for item in index.files}
        # Only ever read when a single file overruns the whole budget, which is
        # rare — but the index is already in hand here, and re-loading it inside
        # the loop would be a second full parse of the same JSON.
        file_outlines = {item.path: outline_of(item.symbols) for item in index.files}
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
            # Tasks replaced by the slices cut out of them. `_load_plan` rebuilds
            # the plan from every persisted row, so without this a resumed
            # mission re-runs the oversized parent it already gave up on.
            superseded = set(
                session.scalars(
                    select(Task.id).where(
                        Task.mission_id == workspace.mission_id,
                        Task.status == TaskStatus.CANCELLED.value,
                    )
                ).all()
            )
        # A task that fails no longer aborts the whole mission: only its dependents
        # are skipped, so independent tasks still run and commit. The mission is
        # reported failed at the end if anything did not complete, but with the
        # completed work preserved rather than thrown away at the first failure.
        failed: dict[str, str] = {}
        skipped: dict[str, list[str]] = {}
        # How many times each *root* task has been cut down. Keyed by root rather
        # than by immediate parent so repeated splitting of one planned task is
        # bounded however deep the slicing goes. Recovered from the slice ids in
        # the rebuilt plan rather than started at zero: a mission resumed after a
        # split used to forget the count, so `_MAX_SPLIT_GENERATIONS` could be
        # spent again on every restart and one task sliced without limit.
        generations: dict[str, int] = _generations_from_plan(plan)
        # root task id -> paths its slices have changed but not yet committed.
        deferred: dict[str, list[str]] = self._deferred_from_history(workspace.mission_id, plan)
        # A worklist rather than a materialised list: a split appends its slices
        # to the front, and a `for` over a list built once would never visit them.
        queue: deque[TaskSpec] = deque(validate_task_graph(plan))
        try:
            while queue:
                spec = queue.popleft()
                if spec.id in completed or spec.id in superseded:
                    continue
                blocked_by = sorted(set(spec.dependencies) & (failed.keys() | skipped.keys()))
                if blocked_by:
                    skipped[spec.id] = blocked_by
                    self._update_task(
                        spec.id,
                        TaskStatus.BLOCKED,
                        evidence=[{"blocked_by": blocked_by}],
                    )
                    self.log.emit(
                        "task.skipped",
                        mission_id=workspace.mission_id,
                        task_id=spec.id,
                        blocked_by=blocked_by,
                    )
                    continue
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
                        if item.id not in completed
                        and item.id not in superseded
                        and item.id != spec.id
                    ]
                    self.memory.update_task(
                        persistent.task_id,
                        current_step=spec.title,
                        pending_steps=pending,
                        status=PersistentTaskStatus.IN_PROGRESS,
                    )
                # Refuse an oversized task before spending a turn on it. The
                # planner sizes tasks against this same envelope, so reaching
                # here means its estimate was wrong — a file grew, a glob
                # expanded, or no envelope was available when the plan was made.
                too_big = self._scope_overrun(spec, file_sizes, builder_envelope, context)
                if too_big and await self._replace_with_slices(
                    workspace,
                    plan,
                    queue,
                    spec,
                    file_sizes,
                    builder_envelope,
                    generations,
                    superseded,
                    reason=too_big,
                    gateway=gateway,
                    outlines=file_outlines,
                ):
                    continue
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
                try:
                    implementation, changed = await self._run_builder(
                        workspace,
                        spec,
                        context,
                        gateway,
                        executor,
                        debugger=False,
                        attempts=0,
                    )
                except IncompleteRun as exc:
                    # A stall behind heavy compaction is not a stuck model: the
                    # agent kept losing what it read and re-reading it. The task
                    # is too large for this window, and splitting it is the only
                    # thing that changes the outcome — a retry would thrash
                    # identically. Anything else falls through to a failure.
                    thrashed = (
                        exc.outcome.stop_reason == "stall"
                        and exc.outcome.compactions >= THRASHING_COMPACTIONS
                    )
                    if thrashed and await self._replace_with_slices(
                        workspace,
                        plan,
                        queue,
                        spec,
                        file_sizes,
                        builder_envelope,
                        generations,
                        superseded,
                        gateway=gateway,
                        outlines=file_outlines,
                        reason=(
                            f"the run compacted {exc.outcome.compactions} times without "
                            "making progress"
                        ),
                        overran=True,
                    ):
                        continue
                    self._record_task_failure(workspace, failed, spec, str(exc))
                    continue
                except RuntimeError as exc:
                    self._record_task_failure(workspace, failed, spec, str(exc))
                    continue
                indexer.build()
                # Re-measured, because this task may have created the file the
                # next one is scoped to. Sizing task N+1 against an index taken
                # before task N ran would score a new 20KB module as a nominal
                # new file and wave an oversized task straight through.
                file_sizes = {item.path: item.size for item in indexer.load().files}
                self._update_task(spec.id, TaskStatus.VERIFYING)
                engine = VerificationEngine(workspace.path, runtime)
                # Computed here rather than at commit time because it decides
                # whether this slice verifies at all, not just whether it commits.
                holds_commit = self._awaits_sibling_slices(spec, plan, completed, superseded)
                # The approved task contract is authoritative. A builder may
                # suggest useful checks in ``finish``, but letting those replace
                # the planner's commands allows a malformed ad-hoc one-liner to
                # sink correct code after the planned check already existed.
                #
                # An intermediate slice is the one case with no commands *by
                # construction*: `split_task` gives the parent's checks to the
                # final slice, because running them against a third of a change
                # asserts a failure. Both fallbacks below defeated that — the
                # builder's suggestion, and then discovery — so the suite ran on
                # incomplete work anyway, and either failed the slice for work
                # that had not happened yet or passed and hid the gap.
                commands: list[str] = []
                if not holds_commit:
                    commands = spec.verification_commands or implementation.verification_commands
                    if not commands:
                        commands = engine.discover_commands()
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

                if commands:
                    self.events.publish(
                        TestsStarted(mission_id=workspace.mission_id, commands=commands)
                    )
                    try:
                        report, attempts = await loop.run(
                            commands,
                            repair,
                            observe=observe_report,
                        )
                    except RuntimeError as exc:
                        self._record_task_failure(workspace, failed, spec, str(exc))
                        continue
                else:
                    # Recorded as an explicit skip rather than an empty pass, so
                    # the evidence for this slice says "the final slice checks
                    # this" instead of looking like a clean run of nothing.
                    report, attempts = _deferred_verification(), 0
                    self.log.emit(
                        "task.verification_deferred",
                        mission_id=workspace.mission_id,
                        task_id=spec.id,
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
                    reason = f"Verification failed for {spec.title}: {report.failures[0].summary}"
                    failed[spec.id] = reason
                    self.log.emit(
                        "task.failed",
                        mission_id=workspace.mission_id,
                        task_id=spec.id,
                        error=reason,
                    )
                    continue
                revision = None
                # An intermediate slice is a third of a coherent change, and the
                # verification that would prove it correct belongs to the last
                # slice (`holds_commit`, decided above). Committing it anyway
                # would put a state no check ever passed into the branch's
                # history. The work stays in the working tree, and the final
                # slice commits the whole change at once.
                root = spec.slice_of
                changed_paths = sorted(set(changed) | set(deferred.get(root, ())))
                if holds_commit:
                    # Carried to the final slice. Without this the last slice
                    # would commit only the files it touched itself, and the
                    # earlier slices' work would sit uncommitted in the tree
                    # while the mission reported the task complete.
                    deferred[root] = changed_paths
                elif commit_verified and self.settings.git.auto_commit_verified_tasks:
                    deferred.pop(root, None)
                    title = spec.title.rsplit(" (", 1)[0] if root else spec.title
                    revision = (
                        GitClient(workspace.path).commit(
                            f"{title}\n\nDaino-Mission: {workspace.mission_id}",
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
                        *([{"committed": changed_paths}] if revision else []),
                        # The running total this slice is handing to the next
                        # one. On the row rather than only in memory, because
                        # the process can stop between two slices and the final
                        # slice has to know what the earlier ones left behind.
                        *([{"deferred": changed_paths}] if holds_commit else []),
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
            self._raise_on_incomplete_plan(plan, completed, failed, skipped, superseded)
        finally:
            await runtime.cleanup()

    async def _run_integration_gate(self, workspace: Workspace) -> None:
        """Verify the assembled project once more after every task has finished.

        Each task is verified against its own commands in isolation, so a change
        that only breaks another task's code — a renamed symbol, a moved import —
        can leave every per-task check green while the whole project no longer
        builds. Running the project's discovered checks once over the final tree
        closes that gap before the mission is reported complete.
        """
        if not self.settings.verification.integration_gate:
            return
        runtime = self._runtime(workspace.path)
        await runtime.prepare()
        try:
            engine = VerificationEngine(workspace.path, runtime)
            commands = self.settings.verification.commands or engine.discover_commands()
            self.events.publish(TestsStarted(mission_id=workspace.mission_id, commands=commands))
            report = await engine.run(commands)
        finally:
            await runtime.cleanup()
        with self.database.session() as session:
            session.add(
                VerificationRun(
                    id=new_id("verification"),
                    mission_id=workspace.mission_id,
                    task_id=None,
                    passed=report.passed,
                    report=report.model_dump(mode="json"),
                )
            )
        duration = (report.finished_at - report.started_at).total_seconds()
        self.events.publish(
            TestsCompleted(
                mission_id=workspace.mission_id,
                passed=report.passed,
                passed_count=sum(check.passed for check in report.checks),
                failed_count=len(report.failures),
                duration_seconds=duration,
                failures=[item.model_dump(mode="json") for item in report.failures],
                details={"scope": "integration"},
            )
        )
        if report.passed:
            return
        # A check the runtime could not even launch (its own program is missing)
        # says nothing about the code, so it must not fail an assembled project.
        real = [
            failure
            for failure in report.failures
            if not missing_executable(failure.command, failure.output_excerpt or "")
        ]
        if not real:
            return
        raise RuntimeError(
            "Integration verification failed after all tasks completed: "
            f"{real[0].summary} (command: {real[0].command}). The per-task work is "
            "committed; fix the cross-task breakage and retry."
        )

    #: How many times one planned task may be cut down before the system stops
    #: and says so. Two generations turn a ten-file task into single files on any
    #: window worth running; a third would mean the estimate is wrong in a way
    #: more slicing will not fix.
    _MAX_SPLIT_GENERATIONS = 2

    def _scope_overrun(
        self,
        spec: TaskSpec,
        sizes: dict[str, int],
        envelope: CapabilityEnvelope | None,
        context: ContextBundle,
    ) -> str:
        """Say why this task will not fit, or return "" if it will.

        Checked before the turn is spent, because the alternative is finding out
        by watching the agent thrash for nine steps.
        """
        if envelope is None:
            return ""
        measurement = measure_scope(spec, sizes)
        if not measurement.fits(envelope):
            return (
                f"its scope of {measurement.entries} files (~{measurement.tokens} tokens) "
                f"exceeds what {envelope.profile_name} can hold "
                f"({envelope.max_files_per_task} files, "
                f"{envelope.task_source_budget_tokens} tokens)"
            )
        # The compiler reports a scoped file it could not fit. That file is one
        # the task is required to edit, so the agent would be working blind on it.
        lost = [note for note in context.omitted_context if "in task scope" in note]
        if lost:
            return f"the context budget could not hold a file the task is scoped to ({lost[0]})"
        return ""

    async def _replace_with_slices(
        self,
        workspace: Workspace,
        plan: TaskPlan,
        queue: deque[TaskSpec],
        spec: TaskSpec,
        sizes: dict[str, int],
        envelope: CapabilityEnvelope | None,
        generations: dict[str, int],
        superseded: set[str],
        *,
        reason: str,
        overran: bool = False,
        gateway: ModelGateway | None = None,
        outlines: dict[str, str] | None = None,
    ) -> bool:
        """Cut *spec* into slices and put them at the front of the worklist.

        Returns False when the task cannot be split, in which case the caller
        carries on and fails it in the ordinary way. Everything here happens
        together or not at all: a plan holding slices whose parent still exists,
        or dependents pointing at a cancelled task, does not validate.

        ``overran`` says the task was measured as fitting and thrashed anyway —
        the field case exactly, where the estimate was optimistic rather than the
        task oversized. Splitting against the same envelope that already said
        "this fits" would return nothing at all, so the budget is tightened to
        the extent the evidence contradicts it.
        """
        if envelope is None:
            return False
        root = spec.slice_of or spec.id
        generation = generations.get(root, 0) + 1
        if generation > self._MAX_SPLIT_GENERATIONS:
            return False
        target = _tightened(envelope, generation) if overran else envelope
        slices, needs_replan = split_task(spec, sizes, target, generation=generation)
        if not slices and needs_replan:
            # One file that overruns the budget on its own. Packing files into
            # groups has nothing left to do; the split has to run through the
            # file, which is a judgement about what belongs together rather than
            # arithmetic. This is the only place the model is asked.
            slices = await self._resize_with_model(workspace, spec, target, gateway, outlines or {})
        if not slices:
            return False

        remaining = [task for task in plan.tasks if task.id != spec.id]
        # Dependents must follow the *last* slice: the change is only complete
        # when every slice has run, and a dependency on the cancelled parent
        # fails validation outright.
        for task in remaining:
            if spec.id in task.dependencies:
                task.dependencies[:] = [
                    slices[-1].id if item == spec.id else item for item in task.dependencies
                ]
        candidate = TaskPlan(summary=plan.summary, mode=plan.mode, tasks=[*remaining, *slices])
        try:
            validate_task_graph(candidate)
        except ValueError as exc:
            # Fail fast rather than persisting a graph the loop cannot execute.
            self.log.emit(
                "task.split_rejected",
                mission_id=workspace.mission_id,
                task_id=spec.id,
                error=str(exc),
            )
            return False

        with self.database.session() as session:
            parent = session.get(Task, spec.id)
            if parent is not None:
                parent.status = TaskStatus.CANCELLED.value
                parent.evidence = [{"split_into": [item.id for item in slices], "reason": reason}]
            for item in slices:
                session.add(
                    Task(
                        id=item.id,
                        mission_id=workspace.mission_id,
                        title=item.title,
                        objective=item.objective,
                        status=TaskStatus.PENDING.value,
                        risk_level=item.risk_level,
                        specification=item.model_dump(mode="json"),
                        assigned_model=item.assigned_model,
                    )
                )
            for item in slices:
                for dependency in item.dependencies:
                    session.add(TaskDependency(task_id=item.id, depends_on_id=dependency))
            # The dependents' rows are rewritten to match the remapping above,
            # or a resume rebuilds the old edge to a task that no longer runs.
            for task in remaining:
                if slices[-1].id in task.dependencies:
                    session.execute(
                        delete(TaskDependency).where(
                            TaskDependency.task_id == task.id,
                            TaskDependency.depends_on_id == spec.id,
                        )
                    )
                    session.add(TaskDependency(task_id=task.id, depends_on_id=slices[-1].id))

        plan.tasks[:] = candidate.tasks
        superseded.add(spec.id)
        generations[root] = generation
        queue.extendleft(reversed(slices))
        self.log.emit(
            "task.split",
            mission_id=workspace.mission_id,
            task_id=spec.id,
            reason=reason,
            slices=[item.id for item in slices],
        )
        self.events.publish(
            TaskSplit(
                mission_id=workspace.mission_id,
                task_id=spec.id,
                title=spec.title,
                reason=reason,
                slices=[item.id for item in slices],
            )
        )
        return True

    async def _resize_with_model(
        self,
        workspace: Workspace,
        spec: TaskSpec,
        envelope: CapabilityEnvelope,
        gateway: ModelGateway | None,
        outlines: dict[str, str],
    ) -> list[TaskSpec]:
        """Ask the planner to cut one oversized file along its own structure."""
        if gateway is None or not self._role_available(ModelRole.PLANNER):
            return []
        paths = [path for path in [*spec.expected_files, *spec.allowed_files] if "*" not in path]
        if len(set(paths)) != 1:
            return []
        try:
            slices = await Planner(gateway).resize(
                workspace.mission_id, spec, envelope, outlines.get(paths[0], "")
            )
        except Exception as exc:  # noqa: BLE001 - a failed re-plan is not a failed mission
            self.log.emit(
                "task.resize_failed",
                mission_id=workspace.mission_id,
                task_id=spec.id,
                error=str(exc),
            )
            return []
        return slices

    @staticmethod
    def _awaits_sibling_slices(
        spec: TaskSpec,
        plan: TaskPlan,
        completed: set[str],
        superseded: set[str],
    ) -> bool:
        """True while a later slice of the same task has still to run.

        Derived from the plan rather than remembered in a set, so it survives a
        resume: the plan is rebuilt from the database and this answer has to be
        the same on both sides of a restart.
        """
        if not spec.slice_of:
            return False
        return any(
            task.slice_of == spec.slice_of
            # Slice ids are zero-padded, so ordering them as strings is ordering
            # them as slices — including across generations, where a re-split
            # slice's children carry its id as their prefix.
            and task.id > spec.id
            and task.id not in completed
            and task.id not in superseded
            for task in plan.tasks
        )

    def _deferred_from_history(self, mission_id: str, plan: TaskPlan) -> dict[str, list[str]]:
        """Work earlier slices left uncommitted, read back after a restart.

        An intermediate slice holds its commit for the final one and hands the
        paths it changed along in a dict on the stack. Restarting between two
        slices emptied that dict, so the last slice committed only the files it
        had touched itself while everything the earlier slices wrote stayed
        uncommitted in the tree — and the mission reported the task complete.

        The rows already record it, so nothing new is stored: each held slice
        writes the running total, and a slice that commits clears it. Replaying
        those in id order reproduces exactly what the in-memory dict held.
        """
        roots = {task.id: task.slice_of for task in plan.tasks if task.slice_of}
        if not roots:
            return {}
        with self.database.session() as session:
            history = sorted(
                (
                    (row.id, list(row.evidence or []))
                    for row in session.scalars(
                        select(Task).where(
                            Task.mission_id == mission_id,
                            Task.status == TaskStatus.COMPLETED.value,
                        )
                    ).all()
                    if row.id in roots
                ),
                key=lambda item: item[0],
            )
        pending: dict[str, list[str]] = {}
        for task_id, evidence in history:
            root = roots[task_id]
            for entry in evidence:
                if not isinstance(entry, dict):
                    continue
                if "committed" in entry:
                    pending.pop(root, None)
                elif "deferred" in entry:
                    pending[root] = [str(path) for path in entry["deferred"] or []]
        return pending

    def _record_task_failure(
        self,
        workspace: Workspace,
        failed: dict[str, str],
        spec: TaskSpec,
        reason: str,
    ) -> None:
        """Mark a task failed and record why, without aborting sibling tasks."""
        failed[spec.id] = reason
        self._update_task(spec.id, TaskStatus.FAILED, evidence=[{"error": reason}])
        self.log.emit(
            "task.failed",
            mission_id=workspace.mission_id,
            task_id=spec.id,
            error=reason,
        )

    def _raise_on_incomplete_plan(
        self,
        plan: TaskPlan,
        completed: set[str],
        failed: dict[str, str],
        skipped: dict[str, list[str]],
        superseded: set[str],
    ) -> None:
        """Fail the mission with a precise breakdown when any task did not finish.

        Completed tasks have already been verified and committed, so the message
        names what got done and what did not rather than discarding the run.
        """
        if not failed and not skipped:
            return
        by_id = {task.id: task for task in plan.tasks}
        # A task replaced by its slices is not outstanding work — the slices are.
        # Counting it would report one fewer completed than actually finished.
        live = [task for task in plan.tasks if task.id not in superseded]
        done = sum(1 for task in live if task.id in completed)
        parts = [f"Completed {done} of {len(live)} tasks."]
        failed_titles = [by_id[task_id].title for task_id in failed if task_id in by_id]
        skipped_titles = [by_id[task_id].title for task_id in skipped if task_id in by_id]
        if failed_titles:
            parts.append("Failed: " + "; ".join(failed_titles) + ".")
        if skipped_titles:
            parts.append("Skipped after a dependency failed: " + "; ".join(skipped_titles) + ".")
        parts.append(
            "Completed tasks were verified and committed; review them, then narrow or "
            "reword the failed tasks and retry."
        )
        raise RuntimeError(" ".join(parts))

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
            # IncompleteRun rather than RuntimeError so the caller can read
            # *why* — a task that overran this model's window is splittable,
            # and a genuinely stuck one is not. Both were previously flattened
            # into the same string.
            raise IncompleteRun(
                describe_incomplete_outcome(
                    outcome,
                    role_label=role.value,
                    pinned=bool(getattr(gateway, "profile_override", "")),
                ),
                outcome,
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


#: The splitting round baked into a slice id by ``_slice`` — ``…-s2-01`` is the
#: first slice of the second round.
_SLICE_GENERATION = re.compile(r"-s(\d+)-\d+")


def _deferred_verification() -> VerificationReport:
    """The report for a slice whose checks belong to the final slice.

    Passing, because nothing failed — but carrying a skipped check that says so,
    so the stored evidence cannot be read as "this third of the change was
    verified".
    """
    now = datetime.now(UTC)
    return VerificationReport(
        passed=True,
        checks=[
            VerificationCheck(
                name="deferred",
                command="(no checks for an intermediate slice)",
                passed=True,
                skipped=True,
                skip_reason=(
                    "This is one slice of a larger change. The task's verification "
                    "runs on the final slice, against the whole change."
                ),
            )
        ],
        started_at=now,
        finished_at=now,
    )


def _slice_generation(task_id: str) -> int:
    """Which splitting round produced this id. 0 for a task that was never cut."""
    return max((int(found) for found in _SLICE_GENERATION.findall(task_id)), default=0)


def _generations_from_plan(plan: TaskPlan) -> dict[str, int]:
    """How many times each root task has already been cut down.

    Derived from the ids rather than remembered, for the same reason
    :meth:`_awaits_sibling_slices` is: the plan is rebuilt from the database on
    a resume, so anything kept only on the stack is silently zero afterwards —
    and a zeroed counter means the split limit can be spent again on every
    restart, which is no limit at all.
    """
    counts: dict[str, int] = {}
    for task in plan.tasks:
        if not task.slice_of:
            continue
        counts[task.slice_of] = max(counts.get(task.slice_of, 0), _slice_generation(task.id))
    return counts


def _tightened(envelope: CapabilityEnvelope, generation: int) -> CapabilityEnvelope:
    """Halve the budget once per splitting round.

    Used only when a task that measured as fitting stalled anyway. The estimate
    has been contradicted by an actual run, so the next attempt is sized by the
    evidence rather than by the same arithmetic that was already wrong — and
    halving each round means a task cannot be re-split into the same shape twice.
    """
    factor = 0.5**generation
    return replace(
        envelope,
        max_files_per_task=max(1, int(envelope.max_files_per_task * factor)),
        task_source_budget_tokens=max(1, int(envelope.task_source_budget_tokens * factor)),
    )


def _action_summary(result: ToolResult) -> str:
    data = result.data or {}
    if not data:
        return "ok"
    parts = [f"{key}: {value}" for key, value in data.items() if key != "content"]
    return "; ".join(parts) or "ok"


def _append_unique(values: list[str], value: str) -> list[str]:
    return list(dict.fromkeys([*values, value]))
