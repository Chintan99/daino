"""Controlled specialist agents; each receives role-specific context."""

from __future__ import annotations

from vasuki.agents.gateway import ModelGateway
from vasuki.model_router import ModelRole
from vasuki.prompts import REVIEWER_SYSTEM
from vasuki.schemas import (
    ContextBundle,
    FailureReport,
    Message,
    RequirementSpec,
    ReviewReport,
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
