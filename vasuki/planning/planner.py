"""Mode recommendation and dependency-aware task planning."""

from __future__ import annotations

from vasuki.agents.gateway import ModelGateway
from vasuki.model_router import ModelRole
from vasuki.prompts import PLANNER_SYSTEM
from vasuki.schemas import Message, ProjectMode, RequirementSpec, TaskPlan, TaskSpec
from vasuki.utils.ids import new_id


def recommend_mode(request: str) -> ProjectMode:
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
    ) -> TaskPlan:
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
                        f"Requirements:\n{requirements.model_dump_json(indent=2)}\n"
                        f"Repository:\n{repository_summary}"
                    ),
                ),
            ],
            TaskPlan,
        )
        return plan.model_copy(update={"mode": mode})


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
