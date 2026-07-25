"""Controlled specialist agents; each receives role-specific context."""

from __future__ import annotations

from vasuki.agents.gateway import ModelGateway
from vasuki.model_router import ModelRole, RoutingContext
from vasuki.prompts import BUILDER_SYSTEM, DEBUGGER_SYSTEM, REVIEWER_SYSTEM
from vasuki.schemas import (
    ContextBundle,
    FailureReport,
    Implementation,
    Message,
    RequirementSpec,
    ReviewReport,
)


class BuilderAgent:
    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def implement(
        self, mission_id: str, context: ContextBundle, *, debugger: bool = False, attempts: int = 0
    ) -> Implementation:
        system = DEBUGGER_SYSTEM if debugger else BUILDER_SYSTEM
        role = ModelRole.DEBUGGER if debugger else ModelRole.BUILDER
        return await self.gateway.structured(
            mission_id,
            role,
            [
                Message(role="system", content=system),
                Message(role="user", content=context.model_dump_json(indent=2)),
            ],
            Implementation,
            routing_context=RoutingContext(
                failed_attempts=attempts,
                affected_files=len(context.included_paths),
                tests_failing=debugger,
            ),
            included_files=context.included_paths,
        )


class ReviewerAgent:
    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def review(
        self,
        mission_id: str,
        requirements: RequirementSpec,
        acceptance_criteria: list[str],
        diff: str,
        verification: str,
    ) -> ReviewReport:
        return await self.gateway.structured(
            mission_id,
            ModelRole.REVIEWER,
            [
                Message(role="system", content=REVIEWER_SYSTEM),
                Message(
                    role="user",
                    content=(
                        f"Requirements:\n{requirements.model_dump_json(indent=2)}\n\n"
                        f"Acceptance criteria:\n{acceptance_criteria}\n\n"
                        f"Git diff:\n{diff}\n\nVerification evidence:\n{verification}"
                    ),
                ),
            ],
            ReviewReport,
        )


def failure_to_context(context: ContextBundle, failure: FailureReport) -> ContextBundle:
    return context.model_copy(update={"failure_summary": failure.model_dump_json(indent=2)})
