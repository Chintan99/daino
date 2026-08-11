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
    AgentAction,
    ProjectMode,
    RequirementSpec,
    ReviewFinding,
    ReviewReport,
    TaskPlan,
    TaskSpec,
)


def _role_name(role: object) -> str:
    return str(getattr(role, "value", role))


class DeterministicGateway:
    def __init__(self, command: str, *, repair: bool = False) -> None:
        self.command = command
        self.repair = repair
        self.implementation_calls = 0
        self._actions = self._script()

    def _script(self) -> dict[str, list[dict[str, Any]]]:
        write_content = (
            "def answer():\n    return 0\n" if self.repair else "def answer():\n    return 42\n"
        )
        builder: list[dict[str, Any]] = [
            {
                "action": "write",
                "path": "feature.py",
                "content": write_content,
                "thought": "Create the feature module.",
            },
            {
                "action": "run_command",
                "command": self.command,
                "thought": "Run the task's executable check before finishing.",
            },
            {
                "action": "finish",
                "summary": "Implement answer",
                "verification_commands": ["vasuki-builder-check-does-not-exist"],
                "thought": "The file is written.",
            },
        ]
        debugger: list[dict[str, Any]] = [
            {
                "action": "replace",
                "path": "feature.py",
                "old_string": "    return 0\n",
                "new_string": "    return 42\n",
                "thought": "Correct the failing return value.",
            },
            {
                "action": "run_command",
                "command": self.command,
                "thought": "Confirm the repair before finishing.",
            },
            {
                "action": "finish",
                "summary": "Repair answer",
                "verification_commands": ["vasuki-builder-check-does-not-exist"],
                "thought": "The value is corrected.",
            },
        ]
        return {"builder": builder, "debugger": debugger}

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
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="Verified and scoped")
        if schema is AgentAction:
            queue = self._actions.get(_role_name(role)) or self._actions["builder"]
            step = queue.pop(0) if queue else dict(self._actions["builder"][-1])
            if step["action"] == "finish":
                self.implementation_calls += 1
            return AgentAction(**step)
        raise AssertionError(schema)


class ReviewRepairGateway:
    """Reject the first review and verify that its finding is repaired and re-reviewed."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.review_calls = 0
        self.actions = [
            AgentAction(
                thought="create",
                action="write",
                path="feature.py",
                content="def answer():\n    return 41\n",
            ),
            AgentAction(thought="done", action="finish", summary="Initial implementation"),
            AgentAction(
                thought="repair",
                action="replace",
                path="feature.py",
                old_string="    return 41\n",
                new_string="    return 42\n",
            ),
            AgentAction(thought="done", action="finish", summary="Review finding repaired"),
        ]

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
                problem_statement="Implement answer",
                goals=["Return the correct answer"],
                functional_requirements=["answer returns 42"],
                acceptance_criteria=["answer returns 42"],
                test_strategy=[self.command],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="Implement answer",
                mode=ProjectMode.DIRECT,
                tasks=[
                    TaskSpec(
                        id="feature",
                        title="Implement answer",
                        objective="Create feature.answer",
                        expected_files=["feature.py"],
                        allowed_files=["feature.py"],
                        acceptance_criteria=["answer returns 42"],
                        verification_commands=[self.command],
                    )
                ],
            )
        if schema is AgentAction:
            return self.actions.pop(0)
        if schema is ReviewReport:
            self.review_calls += 1
            if self.review_calls == 1:
                return ReviewReport(
                    approved=False,
                    summary="answer is off by one",
                    findings=[
                        ReviewFinding(
                            severity="medium",
                            category="correctness",
                            message="answer must return 42",
                            file="feature.py",
                            line=2,
                        )
                    ],
                )
            return ReviewReport(approved=True, summary="finding corrected")
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
    assert not any(path.endswith(".pyc") for path in git(workspace, "ls-files").splitlines())


@pytest.mark.asyncio
async def test_failure_is_repaired_within_limit(git_repo: Path) -> None:
    service = make_service(git_repo, repair=True, review=False)
    mission, _ = await service.run("Add answer feature with repair", ProjectMode.DIRECT)
    workspace = Path(mission.workspace_path or "")
    assert mission.status == "completed"
    assert "return 42" in (workspace / "feature.py").read_text(encoding="utf-8")
    assert service.gateway.implementation_calls == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rejected_review_is_repaired_and_reviewed_again(git_repo: Path) -> None:
    service = make_service(git_repo, repair=False, review=True)
    command = f"{sys.executable} -m py_compile feature.py"
    gateway = ReviewRepairGateway(command)
    service.gateway = gateway  # type: ignore[assignment]

    mission, _ = await service.run("Implement answer", ProjectMode.DIRECT)

    workspace = Path(mission.workspace_path or "")
    assert mission.status == "completed"
    assert "return 42" in (workspace / "feature.py").read_text(encoding="utf-8")
    assert gateway.review_calls == 2
