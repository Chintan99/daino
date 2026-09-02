"""Mode recommendation and dependency-aware task planning."""

from __future__ import annotations

from daino.agents.gateway import ModelGateway
from daino.context.profiles import CapabilityEnvelope
from daino.model_router import ModelRole
from daino.prompts import PLANNER_RESIZE_SYSTEM, PLANNER_SYSTEM
from daino.schemas import (
    Message,
    ProjectMode,
    RepositorySymbol,
    RequirementSpec,
    TaskPlan,
    TaskSpec,
    TaskStatus,
)
from daino.utils.ids import new_id


def recommend_mode(request: str) -> ProjectMode:
    """Choose a planning depth from the shape of the request.

    Deliberately not model-aware, unlike task sizing below. This runs in
    ``MissionService.create`` before any gateway exists, and the mode describes
    how much structure the *request* needs, not how much context the executor
    has. A small-window model promoting every short request to PROGRAM would
    multiply planning calls without making a single task any smaller — which is
    the thing that actually has to shrink.
    """
    lower = request.lower()
    program_markers = ("entire application", "migration", "multi-phase", "redesign", "platform")
    specification_markers = ("add ", "implement ", "authentication", "api", "database", "feature")
    if len(request) > 1200 or any(marker in lower for marker in program_markers):
        return ProjectMode.PROGRAM
    if len(request) > 180 or any(marker in lower for marker in specification_markers):
        return ProjectMode.SPECIFICATION
    return ProjectMode.DIRECT


class Planner:
    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self.gateway = gateway

    async def plan(
        self,
        mission_id: str,
        requirements: RequirementSpec,
        repository_summary: str,
        mode: ProjectMode,
        *,
        envelope: CapabilityEnvelope | None = None,
    ) -> TaskPlan:
        """Plan tasks the *executor* can hold, which may not be this model.

        ``envelope`` describes the builder, not the planner. The shipped
        configuration pairs a strong cloud planner with a local builder, and
        without this the planner wrote tasks in its own terms — a scope the
        executor could not fit, which it then thrashed on and died.

        Keyword-only with a ``None`` default so the duck-typed gateways in the
        test suite, which expose only ``structured``, keep working.
        """
        if self.gateway is None:
            task = TaskSpec(
                id=new_id("task"),
                title="Implement requested change",
                objective=requirements.goals[0],
                risk_level="low" if mode == ProjectMode.DIRECT else "medium",
                acceptance_criteria=requirements.acceptance_criteria,
                verification_commands=[],
                rollback_notes="Restore the mission checkpoint or revert the mission commit.",
            )
            return TaskPlan(summary=requirements.problem_statement, mode=mode, tasks=[task])
        plan = await self.gateway.structured(
            mission_id,
            ModelRole.PLANNER,
            [
                Message(role="system", content=PLANNER_SYSTEM),
                Message(
                    role="user",
                    content=(
                        f"Operating mode: {mode.value}\n"
                        # In the user message rather than the system prompt: the
                        # numbers change per run and per routed model, and
                        # providers cache on leading bytes — the same reason
                        # ToolLoop freezes its message head.
                        f"{_executor_limits(envelope)}"
                        f"Requirements:\n{requirements.model_dump_json(indent=2)}\n"
                        f"Repository:\n{repository_summary}"
                    ),
                ),
            ],
            TaskPlan,
        )
        return plan.model_copy(update={"mode": mode})


    async def resize(
        self,
        mission_id: str,
        task: TaskSpec,
        envelope: CapabilityEnvelope,
        file_outline: str,
    ) -> list[TaskSpec]:
        """Split one task along the structure of the single file it is stuck on.

        The deterministic splitter packs files into groups, which cannot help
        when the scope *is* one file that overruns the budget by itself. Cutting
        inside a file is a semantic judgement — which functions belong together —
        so it is the one case worth another model call.

        Returns an empty list when the model declines or answers unusably; the
        caller then fails the task in the ordinary way.
        """
        if self.gateway is None:
            return []
        plan = await self.gateway.structured(
            mission_id,
            ModelRole.PLANNER,
            [
                Message(role="system", content=PLANNER_RESIZE_SYSTEM),
                Message(
                    role="user",
                    content=(
                        f"{_executor_limits(envelope)}"
                        f"Task to split:\n{task.model_dump_json(indent=2)}\n"
                        f"Outline of the file it edits:\n{file_outline}"
                    ),
                ),
            ],
            TaskPlan,
        )
        if len(plan.tasks) < 2:
            # One task is the original under a new name, which would loop.
            return []
        root = task.slice_of or task.id
        # Ids and dependencies are rebuilt from position, never taken from the
        # model: a returned id that collides with a real task, or a dependency on
        # an id it invented, fails `validate_task_graph` for the whole mission.
        slices: list[TaskSpec] = []
        for index, item in enumerate(plan.tasks):
            final = index == len(plan.tasks) - 1
            slices.append(
                item.model_copy(
                    update={
                        "id": f"{task.id}-r{index + 1:02d}",
                        "dependencies": (
                            list(task.dependencies) if index == 0 else [slices[index - 1].id]
                        ),
                        # The model is told to scope every part to the same file;
                        # this is what makes that true rather than requested.
                        "expected_files": list(task.expected_files),
                        "allowed_files": list(task.allowed_files),
                        "verification_commands": (
                            list(task.verification_commands) if final else []
                        ),
                        "acceptance_criteria": (
                            list(task.acceptance_criteria)
                            if final
                            else item.acceptance_criteria or ["This part is complete."]
                        ),
                        "slice_of": root,
                        "assigned_model": envelope.profile_name,
                        "status": TaskStatus.PENDING,
                        "attempt_count": 0,
                        "evidence": [],
                    }
                )
            )
        return slices


def outline_of(symbols: list[RepositorySymbol]) -> str:
    """Render a file's indexed symbols so a split can follow real boundaries."""
    if not symbols:
        return "(no symbols indexed for this file)"
    return "\n".join(
        f"- line {symbol.line}: {symbol.kind} {symbol.name}{symbol.signature or ''}"
        for symbol in sorted(symbols, key=lambda item: item.line)
    )


def _executor_limits(envelope: CapabilityEnvelope | None) -> str:
    if envelope is None:
        return ""
    return (
        "Executor limits — the model that will run these tasks, not you.\n"
        "The repository map below gives each file's size in bytes; keep each task within these:\n"
        f"{envelope.describe()}\n\n"
    )


def validate_task_graph(plan: TaskPlan) -> list[TaskSpec]:
    """Validate references and return a deterministic topological order."""
    by_id = {task.id: task for task in plan.tasks}
    if len(by_id) != len(plan.tasks):
        raise ValueError("Task ids must be unique")
    for task in plan.tasks:
        unknown = set(task.dependencies) - by_id.keys()
        if unknown:
            raise ValueError(f"Task {task.id} has unknown dependencies: {sorted(unknown)}")
    ordered: list[TaskSpec] = []
    remaining = dict(by_id)
    while remaining:
        ready = [
            task
            for task in remaining.values()
            if set(task.dependencies) <= {item.id for item in ordered}
        ]
        if not ready:
            raise ValueError("Task dependency graph contains a cycle")
        for task in sorted(ready, key=lambda item: item.id):
            ordered.append(task)
            remaining.pop(task.id)
    return ordered
