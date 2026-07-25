"""Persistable requirements compilation."""

from __future__ import annotations

from vasuki.agents.gateway import ModelGateway
from vasuki.model_router import ModelRole
from vasuki.prompts import ARCHITECT_SYSTEM
from vasuki.schemas import Message, RequirementSpec


class RequirementsCompiler:
    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self.gateway = gateway

    async def compile(
        self, mission_id: str, request: str, repository_summary: str
    ) -> RequirementSpec:
        if self.gateway is None:
            return RequirementSpec(
                problem_statement=request,
                goals=[request],
                functional_requirements=[f"Implement the requested behavior: {request}"],
                non_functional_requirements=[
                    "Preserve existing behavior outside the requested scope",
                    "Keep the implementation maintainable and testable",
                ],
                acceptance_criteria=[
                    "The requested behavior is implemented",
                    "Relevant automated verification passes",
                    "No unrelated files are changed",
                ],
                test_strategy=["Run targeted tests", "Run the repository's standard checks"],
                assumptions=["No provider configured; requirements were compiled locally"],
            )
        return await self.gateway.structured(
            mission_id,
            ModelRole.ARCHITECT,
            [
                Message(role="system", content=ARCHITECT_SYSTEM),
                Message(
                    role="user",
                    content=f"Request:\n{request}\n\nRepository summary:\n{repository_summary}",
                ),
            ],
            RequirementSpec,
        )
