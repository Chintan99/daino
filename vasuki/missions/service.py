"""End-to-end mission orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from vasuki.agents import BuilderAgent, ModelGateway, ReviewerAgent
from vasuki.config.models import Settings
from vasuki.context import ContextCompiler
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
    FailureReport,
    MissionStatus,
    ProjectMode,
    RequirementSpec,
    TaskPlan,
    TaskSpec,
    TaskStatus,
)
from vasuki.security import PolicyEngine
from vasuki.tools import EditTools
from vasuki.utils.ids import new_id
from vasuki.verification import RepairLoop, VerificationEngine
from vasuki.workspace import Workspace, WorkspaceManager


class MissionService:
    """Runs sequential, verification-gated tasks in isolated Git worktrees."""

    def __init__(self, root: Path, settings: Settings, database: Database) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.database = database
        self.log = AuditLog(self.root)
        self.workspace_manager = WorkspaceManager(self.root)
        self.gateway = ModelGateway(settings, database)

    def _role_available(self, role: ModelRole) -> bool:
        profile_name = self.settings.routing.get(role.value)
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
        return mission

    async def plan(
        self, request: str, mode: ProjectMode | None = None
    ) -> tuple[Mission, RequirementSpec, TaskPlan]:
        mission = self.create(request, mode)
        return await self._plan_existing(mission)

    async def _plan_existing(self, mission: Mission) -> tuple[Mission, RequirementSpec, TaskPlan]:
        self._update_mission(mission.id, status=MissionStatus.PLANNING.value)
        indexer = RepositoryIndexer(self.root)
        summary = indexer.summary()
        architect_gateway = self.gateway if self._role_available(ModelRole.ARCHITECT) else None
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
        planner_gateway = self.gateway if self._role_available(ModelRole.PLANNER) else None
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
        self, request: str, mode: ProjectMode | None = None
    ) -> tuple[Mission, Path | None]:
        mission, requirements, plan = await self.plan(request, mode)
        return await self.execute(mission.id, requirements, plan)

    async def execute(
        self,
        mission_id: str,
        requirements: RequirementSpec | None = None,
        plan: TaskPlan | None = None,
    ) -> tuple[Mission, Path | None]:
        mission = self.get(mission_id)
        if not self._role_available(ModelRole.BUILDER):
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
        try:
            await self._execute_tasks(workspace, requirements, plan)
            evidence_path = await self._review_and_finish(workspace, requirements, plan)
            return self.get(mission.id), evidence_path
        except Exception as exc:
            current = self.get(mission.id)
            if current.status != MissionStatus.BLOCKED.value:
                self._update_mission(
                    mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
                )
            self.log.emit("mission.failed", mission_id=mission.id, error=str(exc))
            raise

    async def _execute_tasks(
        self, workspace: Workspace, requirements: RequirementSpec, plan: TaskPlan
    ) -> None:
        runtime = self._runtime(workspace.path)
        await runtime.prepare()
        indexer = RepositoryIndexer(workspace.path)
        indexer.build()
        compiler = ContextCompiler(
            workspace.path, indexer, self.settings.project.context_budget_tokens
        )
        builder = BuilderAgent(self.gateway)
        memory = MemoryStore(self.database)
        completed: set[str] = set()
        for spec in validate_task_graph(plan):
            if not set(spec.dependencies) <= completed:
                raise RuntimeError(f"Dependencies not completed for {spec.id}")
            self._update_task(spec.id, TaskStatus.RUNNING)
            decisions = memory.relevant_decisions([*spec.expected_files, *spec.allowed_files])
            context = compiler.compile(spec, decisions=decisions)
            implementation = await builder.implement(workspace.mission_id, context)
            editor = EditTools(workspace.path, spec.allowed_files)
            changed: list[str] = []
            for modification in implementation.modifications:
                result = editor.apply_modification(modification)
                if not result.success:
                    raise RuntimeError(f"Rejected modification {modification.path}: {result.error}")
                changed.extend(result.data.get("files", [modification.path]))
                self.log.emit(
                    "tool.edit",
                    mission_id=workspace.mission_id,
                    task_id=spec.id,
                    path=modification.path,
                    action=modification.action,
                )
                with self.database.session() as session:
                    session.add(
                        ToolCall(
                            id=new_id("tool-call"),
                            mission_id=workspace.mission_id,
                            tool=f"edit.{modification.action}",
                            arguments={
                                "path": modification.path,
                                "reason": modification.reason,
                            },
                            result_summary=result.error or "Applied",
                            duration_seconds=result.duration_seconds,
                            success=result.success,
                        )
                    )
            indexer.build()
            self._update_task(spec.id, TaskStatus.VERIFYING)
            engine = VerificationEngine(workspace.path, runtime)
            commands = implementation.verification_commands or spec.verification_commands
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
                refreshed = compiler.compile(
                    current_spec,
                    decisions=current_decisions,
                    failure_summary=failure.model_dump_json(indent=2),
                )
                repair_implementation = await builder.implement(
                    workspace.mission_id,
                    refreshed,
                    debugger=escalated,
                    attempts=attempt,
                )
                any_change = False
                for modification in repair_implementation.modifications:
                    result = current_editor.apply_modification(modification)
                    if not result.success:
                        return False
                    any_change = True
                indexer.build()
                return any_change

            report, attempts = await loop.run(commands, repair)
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
                                "passed"
                                if check.passed
                                else check.result.stderr[-1000:] or check.result.stdout[-1000:]
                            ),
                            duration_seconds=check.result.duration_seconds,
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
            if self.settings.git.auto_commit_verified_tasks:
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
            completed.add(spec.id)
        await runtime.cleanup()

    async def _review_and_finish(
        self, workspace: Workspace, requirements: RequirementSpec, plan: TaskPlan
    ) -> Path:
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
            if not self._role_available(ModelRole.REVIEWER):
                raise RuntimeError(
                    "Independent review is required but no reviewer route is configured"
                )
            review = await ReviewerAgent(self.gateway).review(
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
        final_revision = git.revision()
        self._update_mission(
            workspace.mission_id,
            status=MissionStatus.COMPLETED.value,
            final_revision=final_revision,
        )
        path = EvidenceExporter(self.root, self.database).export(workspace.mission_id, "markdown")
        self.log.emit(
            "mission.completed",
            mission_id=workspace.mission_id,
            commit=final_revision,
            evidence=str(path),
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
