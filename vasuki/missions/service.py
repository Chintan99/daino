"""End-to-end mission orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from vasuki.agents import ModelGateway, ReviewerAgent, ToolLoop
from vasuki.config.models import Settings
from vasuki.context import ContextCompiler
from vasuki.events import (
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
from vasuki.git import GitClient
from vasuki.memory import MemoryStore
from vasuki.missions.evidence import EvidenceExporter
from vasuki.model_router import ModelRole
from vasuki.observability import AuditLog
from vasuki.persistence import Database
from vasuki.persistence.models import (
    Checkpoint,
    Mission,
    RequirementVersion,
    Review,
    Task,
    TaskDependency,
    ToolCall,
    VerificationRun,
)
from vasuki.planning import Planner, recommend_mode, validate_task_graph
from vasuki.repository import RepositoryIndexer
from vasuki.requirements import RequirementsCompiler
from vasuki.runtimes import DockerRuntime, LocalRuntime, Runtime
from vasuki.schemas import (
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
from vasuki.security import PolicyEngine
from vasuki.tools import ActionExecutor, EditTools
from vasuki.utils.ids import new_id
from vasuki.verification import RepairLoop, VerificationEngine
from vasuki.workspace import Workspace, WorkspaceManager


class MissionService:
    """Runs sequential, verification-gated tasks in isolated Git worktrees."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        database: Database,
        events: EventBus | None = None,
    ) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.database = database
        self.log = AuditLog(self.root)
        self.workspace_manager = WorkspaceManager(self.root)
        self.events = events or EventBus()
        self.gateway = ModelGateway(settings, database, self.events)

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

    def create(self, request: str, mode: ProjectMode | None = None) -> Mission:
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
        if not self.workspace_manager.git.is_repository():
            raise RuntimeError(
                "Coding missions need a Git repository. Run `git init` in "
                f"{self.root} and commit once, then retry. Plain questions work "
                "without one."
            )
        self._update_mission(mission.id, status=MissionStatus.PLANNING.value)
        gateway = self._gateway(profile_override)
        indexer = RepositoryIndexer(self.root)
        summary = indexer.summary()
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
    ) -> tuple[Mission, Path | None]:
        mission = self.get(mission_id)
        if mission.status == MissionStatus.RUNNING.value:
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
        workspace = self.workspace_manager.create(
            mission.id,
            mission.request,
            use_worktree=self.settings.git.use_worktrees,
        )
        self._update_mission(
            mission.id,
            status=MissionStatus.RUNNING.value,
            workspace_path=str(workspace.path),
            branch=workspace.branch,
            initial_revision=workspace.initial_revision,
        )
        self.events.publish(
            MissionStarted(
                mission_id=mission.id,
                workspace=str(workspace.path),
                branch=workspace.branch,
            )
        )
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
            raise

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
        compiler = ContextCompiler(
            workspace.path, indexer, self.settings.project.context_budget_tokens
        )
        gateway = self._gateway(profile_override)
        memory = MemoryStore(self.database)
        completed: set[str] = set()
        try:
            for spec in validate_task_graph(plan):
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
                context = compiler.compile(spec, decisions=decisions)
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
                executor = ActionExecutor(editor)
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
                commands = implementation.verification_commands or spec.verification_commands
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
                    refreshed = compiler.compile(
                        current_spec,
                        decisions=current_decisions,
                        failure_summary=failure.model_dump_json(indent=2),
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
                        ActionExecutor(current_editor),
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
                    revision = GitClient(workspace.path).commit(
                        f"{spec.title}\n\nVasuki-Mission: {workspace.mission_id}"
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

        def observe(action: AgentAction, result: ToolResult, paths: list[str]) -> None:
            subject = action.path or action.query or action.summary or action.action
            tool_name = f"agent.{action.action}"
            self.events.publish(
                ToolStarted(
                    mission_id=workspace.mission_id,
                    tool=tool_name,
                    summary=subject,
                    details={"task_id": spec.id, "role": role.value},
                )
            )
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

        outcome = await ToolLoop(
            gateway,
            role,
            executor,
            debugger=debugger,
            attempts=attempts,
        ).run(workspace.mission_id, context, on_action=observe)
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
        diff = git.diff(workspace.initial_revision)
        with self.database.session() as session:
            verifications = session.scalars(
                select(VerificationRun).where(VerificationRun.mission_id == workspace.mission_id)
            ).all()
            verification_json = json.dumps(
                [item.report for item in verifications], indent=2, default=str
            )
        if self.settings.verification.require_review:
            if not self._role_available(ModelRole.REVIEWER, profile_override):
                raise RuntimeError(
                    "Independent review is required but no reviewer route is configured"
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
            if not review.approved:
                self._update_mission(
                    workspace.mission_id,
                    status=MissionStatus.BLOCKED.value,
                    failure=review.summary,
                )
                raise RuntimeError(f"Independent review rejected the mission: {review.summary}")
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
        final_revision = git.commit(f"{mission.request[:72]}\n\nVasuki-Mission: {mission_id}")
        self._update_mission(
            mission_id,
            status=MissionStatus.COMPLETED.value,
            final_revision=final_revision,
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
