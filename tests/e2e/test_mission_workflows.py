from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from vasuki.config import default_settings, save_settings
from vasuki.config.models import ModelProfileConfig, ProviderConfig
from vasuki.missions import MissionService
from vasuki.persistence import Database
from vasuki.schemas import (
    Implementation,
    ProjectMode,
    RequirementSpec,
    ReviewReport,
    TaskPlan,
    TaskSpec,
)


class DeterministicGateway:
    def __init__(self, command: str, *, repair: bool = False) -> None:
        self.command = command
        self.repair = repair
        self.implementation_calls = 0

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: object,
        schema: type[Any],
        **kwargs: object,
    ) -> Any:
        if schema is RequirementSpec:
            return RequirementSpec(
                problem_statement="Add a deterministic feature",
                goals=["Expose answer()"],
                functional_requirements=["feature.answer returns 42"],
                acceptance_criteria=["answer() returns 42"],
                test_strategy=[self.command],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="Add feature",
                mode=ProjectMode.DIRECT,
                tasks=[
                    TaskSpec(
                        id="feature",
                        title="Add answer feature",
                        objective="Create feature.answer",
                        expected_files=["feature.py"],
                        allowed_files=["feature.py"],
                        acceptance_criteria=["answer() returns 42"],
                        verification_commands=[self.command],
                    )
                ],
            )
        if schema is Implementation:
            self.implementation_calls += 1
            if self.repair and self.implementation_calls == 1:
                return Implementation(
                    summary="Initial implementation",
                    modifications=[
                        {
                            "path": "feature.py",
                            "action": "create",
                            "content": "def answer():\n    return 0\n",
                            "reason": "Add feature",
                        }
                    ],
                    verification_commands=[self.command],
                )
            if self.repair:
                return Implementation(
                    summary="Repair answer",
                    modifications=[
                        {
                            "path": "feature.py",
                            "action": "patch",
                            "unified_diff": (
                                "--- a/feature.py\n"
                                "+++ b/feature.py\n"
                                "@@ -1,2 +1,2 @@\n"
                                " def answer():\n"
                                "-    return 0\n"
                                "+    return 42\n"
                            ),
                            "reason": "Correct failing value",
                        }
                    ],
                    verification_commands=[self.command],
                )
            return Implementation(
                summary="Implement answer",
                modifications=[
                    {
                        "path": "feature.py",
                        "action": "create",
                        "content": "def answer():\n    return 42\n",
                        "reason": "Add feature",
                    }
                ],
                verification_commands=[self.command],
            )
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="Verified and scoped")
        raise AssertionError(schema)


def make_service(root: Path, *, repair: bool, review: bool) -> MissionService:
    settings = default_settings(root)
    settings.runtime.default = "local"
    settings.verification.require_review = review
    settings.verification.repair_attempts_local = 0
    settings.verification.total_attempts = 2
    settings.providers = {
        "mock": ProviderConfig(type="vllm", base_url="http://mock.invalid/v1", model="mock")
    }
    settings.models = {"mock": ModelProfileConfig(provider="mock", model="mock", local=True)}
    settings.routing = {
        role: "mock"
        for role in (
            "architect",
            "planner",
            "builder",
            "reviewer",
            "debugger",
        )
    }
    save_settings(settings, root)
    database = Database(settings, root)
    database.initialize()
    command = f"{sys.executable} -c 'from feature import answer; assert answer() == 42'"
    service = MissionService(root, settings, database)
    service.gateway = DeterministicGateway(command, repair=repair)  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_local_model_coding_mission_exports_evidence(git_repo: Path) -> None:
    service = make_service(git_repo, repair=False, review=True)
    mission, evidence = await service.run("Add answer feature", ProjectMode.DIRECT)
    assert mission.status == "completed"
    assert evidence is not None and evidence.exists()
    assert "answer feature" in evidence.read_text(encoding="utf-8").lower()
    workspace = Path(mission.workspace_path or "")
    assert (workspace / "feature.py").exists()
    assert git(workspace, "log", "-1", "--pretty=%B").startswith("Add answer feature")


@pytest.mark.asyncio
async def test_failure_is_repaired_within_limit(git_repo: Path) -> None:
    service = make_service(git_repo, repair=True, review=False)
    mission, _ = await service.run("Add answer feature with repair", ProjectMode.DIRECT)
    workspace = Path(mission.workspace_path or "")
    assert mission.status == "completed"
    assert "return 42" in (workspace / "feature.py").read_text(encoding="utf-8")
    assert service.gateway.implementation_calls == 2  # type: ignore[attr-defined]
