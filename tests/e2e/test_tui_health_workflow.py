from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from daino.application import open_project
from daino.config import default_settings, save_settings
from daino.config.models import ModelProfileConfig, ProviderConfig
from daino.persistence import Database
from daino.repository import RepositoryIndexer
from daino.schemas import (
    AgentAction,
    ProjectMode,
    RequirementSpec,
    ReviewReport,
    TaskPlan,
    TaskSpec,
)
from daino.tui.app import DainoApp
from daino.tui.screens.workspace import WorkspaceScreen
from daino.tui.widgets import ApprovalModal
from daino.tui.widgets.message import MessageCard


def _role_name(role: object) -> str:
    return str(getattr(role, "value", role))


class HealthWorkflowGateway:
    """Credential-free model double that exercises the real mission engine."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.implementation_calls = 0
        self._actions = self._script()

    def _script(self) -> dict[str, list[dict[str, Any]]]:
        builder = [
            {
                "action": "write",
                "path": "health.py",
                "content": 'def health() -> tuple[int, str]:\n    return 200, "starting"\n',
                "thought": "Add the requested health endpoint.",
            },
            {
                "action": "finish",
                "summary": "Initial health endpoint",
                "verification_commands": [self.command],
                "thought": "The endpoint is written.",
            },
        ]
        debugger = [
            {
                "action": "replace",
                "path": "health.py",
                "old_string": '    return 200, "starting"\n',
                "new_string": '    return 200, "ok"\n',
                "thought": "Match the approved health contract.",
            },
            {
                "action": "finish",
                "summary": "Repair health status",
                "verification_commands": [self.command],
                "thought": "The status is corrected.",
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
                problem_statement="Add a health endpoint and tests",
                goals=["Expose a deterministic health response"],
                functional_requirements=["health() returns status 200 and ok"],
                acceptance_criteria=["The health verification passes"],
                test_strategy=[self.command],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="Implement health endpoint",
                mode=ProjectMode.SPECIFICATION,
                tasks=[
                    TaskSpec(
                        id="health",
                        title="Add health endpoint and tests",
                        objective="Implement health() and verify its response",
                        expected_files=["health.py"],
                        allowed_files=["health.py"],
                        acceptance_criteria=["health() returns (200, 'ok')"],
                        verification_commands=[self.command],
                    )
                ],
            )
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="Health change is scoped and verified")
        if schema is AgentAction:
            queue = self._actions.get(_role_name(role)) or self._actions["builder"]
            step = queue.pop(0) if queue else dict(self._actions["builder"][-1])
            if step["action"] == "finish":
                self.implementation_calls += 1
            return AgentAction(**step)
        raise AssertionError(schema)


@pytest.mark.asyncio
async def test_complete_health_mission_through_tui_and_restore(
    git_repo: Path,
) -> None:
    settings = default_settings(git_repo)
    settings.runtime.default = "local"
    settings.verification.repair_attempts_local = 0
    settings.verification.total_attempts = 2
    settings.providers = {
        "mock": ProviderConfig(
            type="vllm",
            base_url="http://mock.invalid/v1",
            model="mock",
        )
    }
    settings.models = {"mock": ModelProfileConfig(provider="mock", model="mock", local=True)}
    settings.routing = {
        role: "mock" for role in ("architect", "planner", "builder", "debugger", "reviewer")
    }
    save_settings(settings, git_repo)
    database = Database(settings, git_repo)
    database.initialize()
    database.engine.dispose()
    RepositoryIndexer(git_repo).build()
    command = f"{sys.executable} -c 'from health import health; assert health() == (200, \"ok\")'"

    context = open_project(git_repo)
    app = DainoApp(git_repo, context=context)
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        workspace = app.screen
        assert isinstance(workspace, WorkspaceScreen)
        gateway = HealthWorkflowGateway(command)
        workspace.missions.core.gateway = gateway  # type: ignore[assignment]

        await workspace.execute_command("/plan Add a health endpoint and tests.")
        await pilot.pause(0.5)
        assert isinstance(app.screen, ApprovalModal)
        await pilot.click("#approve-once")

        for _ in range(40):
            await pilot.pause(0.15)
            if (
                isinstance(app.screen, ApprovalModal)
                and workspace.missions.mission_details(workspace.active_mission_id or "")[
                    "mission"
                ]["status"]
                == "awaiting_change_approval"
            ):
                break

        assert isinstance(app.screen, ApprovalModal)
        pending = workspace.missions.mission_details(workspace.active_mission_id or "")
        assert pending["mission"]["final_revision"] is None
        await pilot.click("#approve-once")
        for _ in range(20):
            await pilot.pause(0.15)
            if workspace.active_status == "Completed":
                break

        assert workspace.active_status == "Completed"
        assert gateway.implementation_calls == 2
        mission_id = workspace.active_mission_id or ""
        details = workspace.missions.mission_details(mission_id)
        mission = details["mission"]
        assert mission["status"] == "completed"
        assert mission["final_revision"]
        assert details["tests"][-1]["passed"] is True
        assert (
            Path(mission["workspace_path"], "health.py")
            .read_text(encoding="utf-8")
            .endswith('return 200, "ok"\n')
        )
        contents = [card.raw_content for card in workspace.query(MessageCard)]
        assert any("1 failed" in content for content in contents)
        assert any("1 passed" in content for content in contents)
        assert any("Mission completed" in content for content in contents)
        assert list((git_repo / ".daino" / "artifacts" / mission_id).iterdir())

    reopened = open_project(git_repo)
    try:
        restored = reopened.database.engine.connect()
        restored.close()
        assert (
            WorkspaceScreen(reopened).missions.mission_details(mission_id)["mission"]["status"]
            == "completed"
        )
    finally:
        reopened.close()
