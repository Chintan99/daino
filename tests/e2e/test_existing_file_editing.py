"""Editing files that already exist.

The agent could create files but not change them: whole-file content for an
existing path was rejected as "file exists", any drift in a unified diff was
fatal, and the planner was never shown which files the repository contained.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from daino.application import initialize_project, open_project
from daino.config.models import ModelProfileConfig, ProviderConfig
from daino.context import ContextCompiler
from daino.missions import MissionService
from daino.model_router import ModelRole
from daino.repository import RepositoryIndexer
from daino.runtimes import LocalRuntime
from daino.schemas import (
    AgentAction,
    FileModification,
    ProjectMode,
    RequirementSpec,
    TaskPlan,
    TaskSpec,
)
from daino.security import PolicyEngine
from daino.tools import EditTools
from daino.verification import VerificationEngine

ORIGINAL = """<!DOCTYPE html>
<html>
  <head><title>Old</title></head>
  <body>
    <h1>Old headline</h1>
  </body>
</html>
"""


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def landing_project(root: Path) -> Path:
    (root / "landing.html").write_text(ORIGINAL, encoding="utf-8")
    git_init(root)
    initialize_project(root)
    return root


class RewriteGateway:
    """Returns whole-file content for an existing path, as real models do."""

    def __init__(self, command: str) -> None:
        self.command = command
        self._builder_turn = 0

    def with_profile(self, profile: str) -> RewriteGateway:
        return self

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
                problem_statement="Update the landing page headline",
                goals=["Show the new headline"],
                functional_requirements=["landing.html shows 'New headline'"],
                acceptance_criteria=["landing.html contains New headline"],
                test_strategy=[self.command],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="Edit the landing page",
                mode=ProjectMode.SPECIFICATION,
                tasks=[
                    TaskSpec(
                        id="edit",
                        title="Update the landing page headline",
                        objective="Replace the headline in landing.html",
                        expected_files=["landing.html"],
                        allowed_files=["landing.html"],
                        acceptance_criteria=["landing.html contains New headline"],
                        verification_commands=[self.command],
                    )
                ],
            )
        if schema is AgentAction:
            self._builder_turn += 1
            if self._builder_turn == 1:
                return AgentAction(
                    thought="Rewrite the page with the new headline.",
                    action="write",
                    # Path spelled with a ./ prefix, and whole content rather
                    # than a diff — both of which used to be rejected outright.
                    path="./landing.html",
                    content=ORIGINAL.replace("Old headline", "New headline"),
                )
            return AgentAction(
                thought="The headline is updated.",
                action="finish",
                summary="Rewrite the landing page",
                verification_commands=[self.command],
            )
        raise AssertionError(schema)


@pytest.mark.asyncio
async def test_mission_rewrites_an_existing_file(tmp_path: Path) -> None:
    root = landing_project(tmp_path)
    context = open_project(root)
    context.settings.runtime.default = "local"
    context.settings.verification.require_review = False
    context.settings.providers["stub"] = ProviderConfig(
        type="openai-compatible",
        base_url="http://127.0.0.1:1/v1",
        model="stub",
    )
    context.settings.models["stub"] = ModelProfileConfig(provider="stub", model="stub")
    context.settings.routing = {role.value: "stub" for role in ModelRole}
    service = MissionService(root, context.settings, context.database, context.events)
    service.gateway = RewriteGateway('python -c "pass"')  # type: ignore[assignment]

    mission, _ = await service.run("Change the landing page headline")

    assert mission.workspace_path
    edited = Path(mission.workspace_path) / "landing.html"
    assert "New headline" in edited.read_text(encoding="utf-8")
    assert "Old headline" not in edited.read_text(encoding="utf-8")
    context.close()


def test_planner_is_shown_the_files_that_already_exist(tmp_path: Path) -> None:
    root = landing_project(tmp_path)
    summary = RepositoryIndexer(root).summary()

    # Without this the planner can only invent a filename.
    assert "landing.html" in summary
    assert "Existing files:" in summary
    assert "landing.html" not in RepositoryIndexer(root).summary(include_files=False)


def test_scoped_file_is_included_even_when_it_exceeds_the_budget(tmp_path: Path) -> None:
    root = landing_project(tmp_path)
    (root / "big.html").write_text("<p>x</p>\n" * 5000, encoding="utf-8")
    indexer = RepositoryIndexer(root)
    indexer.build()
    task = TaskSpec(
        id="edit",
        title="Edit the big page",
        objective="Change big.html",
        expected_files=["big.html"],
        allowed_files=["big.html"],
        acceptance_criteria=["it changes"],
        verification_commands=["python -c 'pass'"],
    )

    bundle = ContextCompiler(root, indexer, token_budget=2_000).compile(task)

    assert "big.html" in bundle.files
    assert "truncated to fit the context budget" in bundle.files["big.html"]


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("create", {"content": "<h1>New headline</h1>\n"}),
        ("patch", {"content": "<h1>New headline</h1>\n"}),
        (
            "patch",
            {
                # Miscounted hunk header, which models emit constantly.
                "unified_diff": (
                    "--- a/landing.html\n+++ b/landing.html\n@@ -5 +5 @@\n"
                    "-    <h1>Old headline</h1>\n+    <h1>New headline</h1>\n"
                )
            },
        ),
        (
            "patch",
            {
                # Trailing whitespace drift on a context line.
                "unified_diff": (
                    "--- a/landing.html\n+++ b/landing.html\n@@ -4,3 +4,3 @@\n"
                    "  <body> \n-    <h1>Old headline</h1>\n"
                    "+    <h1>New headline</h1>\n  </body>\n"
                )
            },
        ),
    ],
)
def test_edit_survives_the_shapes_models_actually_emit(
    tmp_path: Path,
    action: str,
    payload: dict[str, str],
) -> None:
    (tmp_path / "landing.html").write_text(ORIGINAL, encoding="utf-8")
    git_init(tmp_path)
    tools = EditTools(tmp_path, allowed_files=["landing.html"])

    result = tools.apply_modification(
        FileModification(path="./landing.html", action=action, reason="edit", **payload)
    )

    assert result.success, result.error
    assert "New headline" in (tmp_path / "landing.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shell_only_checks_are_skipped_rather_than_failing_the_mission(
    tmp_path: Path,
) -> None:
    """A planner's shell one-liner must not sink a mission whose code is fine.

    Commands run without a shell, so `grep -c foo page.html | head` cannot work.
    It used to reach the runtime and abort the run with a confusing
    "head: No such file or directory".
    """
    (tmp_path / "landing.html").write_text(ORIGINAL, encoding="utf-8")
    git_init(tmp_path)
    engine = VerificationEngine(tmp_path, LocalRuntime(tmp_path))

    report = await engine.run(['grep -c "New headline" landing.html | head -1'])

    skipped = [check for check in report.checks if check.skipped]
    assert [check.command for check in skipped] == ['grep -c "New headline" landing.html | head -1']
    assert "shell" in skipped[0].skip_reason
    # It fell back to a check the repository actually supports.
    assert [check.command for check in report.checks if not check.skipped] == ["git diff --check"]
    assert report.passed


def test_shell_syntax_is_refused_with_an_explanation(tmp_path: Path) -> None:
    decision = PolicyEngine().command_decision('grep -c "x" a.html | head -1')

    assert not decision.allowed
    assert "shell syntax is not available" in decision.reasons[0]
    assert PolicyEngine().command_decision("pytest -q").allowed


def test_a_failed_rewrite_restores_the_original_file(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    tools = EditTools(tmp_path, allowed_files=["app.py"])

    result = tools.apply_modification(
        FileModification(
            path="app.py",
            action="create",
            content="def broken(:\n",
            reason="introduce a syntax error",
        )
    )

    assert not result.success
    assert "syntax" in (result.error or "").casefold()
    assert source.read_text(encoding="utf-8") == "def value():\n    return 1\n"
