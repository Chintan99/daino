from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from daino.config import default_settings, save_settings
from daino.config.models import ModelProfileConfig, ProviderConfig
from daino.missions import MissionService
from daino.persistence import Database
from daino.schemas import (
    AgentAction,
    Message,
    ProjectMode,
    RequirementSpec,
    ReviewFinding,
    ReviewReport,
    TaskPlan,
    TaskSpec,
)
from tests.conftest import git


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
                "verification_commands": ["daino-builder-check-does-not-exist"],
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
                "verification_commands": ["daino-builder-check-does-not-exist"],
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


class PartialPlanGateway:
    """Two-task plan where one task fails, to exercise skip-and-continue.

    ``dependent`` links the second task to the first; ``independent`` leaves them
    unrelated. Which file to write is chosen from a marker in the task objective,
    so the gateway is correct regardless of the order the scheduler runs them in.
    """

    def __init__(self, good_cmd: str, bad_cmd: str, *, dependent: bool) -> None:
        self.good_cmd = good_cmd
        self.bad_cmd = bad_cmd
        self.dependent = dependent
        self.written: set[str] = set()

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
                problem_statement="Two tasks, one of which fails",
                goals=["Complete the independent task regardless"],
                functional_requirements=["good.py and bad.py are created"],
                acceptance_criteria=["both modules exist"],
                test_strategy=[self.good_cmd],
            )
        if schema is TaskPlan:
            good = TaskSpec(
                id="good",
                title="Write the good module",
                objective="MARK_GOOD create good.py",
                expected_files=["good.py"],
                allowed_files=["good.py"],
                acceptance_criteria=["good.py exists"],
                verification_commands=[self.good_cmd],
            )
            bad = TaskSpec(
                id="bad",
                title="Write the bad module",
                objective="MARK_BAD create bad.py",
                expected_files=["bad.py"],
                allowed_files=["bad.py"],
                acceptance_criteria=["bad.py exists"],
                verification_commands=[self.bad_cmd],
            )
            if self.dependent:
                # good depends on bad, so a failed bad blocks good entirely.
                good = good.model_copy(update={"dependencies": ["bad"]})
                tasks = [bad, good]
            else:
                tasks = [good, bad]
            return TaskPlan(summary="Two tasks", mode=ProjectMode.SPECIFICATION, tasks=tasks)
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="ok")
        if schema is AgentAction:
            # The current task's objective is the bundle's top-level ``task`` field,
            # which uniquely identifies which task is running even though sibling
            # context can leak elsewhere in the prompt.
            objective = self._current_objective(messages)
            if "MARK_GOOD" in objective and "good.py" not in self.written:
                self.written.add("good.py")
                return AgentAction(
                    thought="write", action="write", path="good.py", content="ok = 'good'\n"
                )
            if "MARK_BAD" in objective and "bad.py" not in self.written:
                self.written.add("bad.py")
                return AgentAction(
                    thought="write", action="write", path="bad.py", content="ok = 'bad'\n"
                )
            return AgentAction(thought="done", action="finish", summary="done")
        raise AssertionError(schema)

    @staticmethod
    def _current_objective(messages: object) -> str:
        for item in messages if isinstance(messages, list) else []:
            if isinstance(item, Message) and item.role == "user":
                try:
                    return str(json.loads(item.content).get("task", ""))
                except (ValueError, TypeError):
                    return item.content
        return ""


def make_partial_service(root: Path, gateway: PartialPlanGateway) -> MissionService:
    settings = default_settings(root)
    settings.runtime.default = "local"
    settings.verification.require_review = False
    settings.verification.repair_attempts_local = 0
    settings.verification.total_attempts = 1
    settings.providers = {
        "mock": ProviderConfig(type="vllm", base_url="http://mock.invalid/v1", model="mock")
    }
    settings.models = {"mock": ModelProfileConfig(provider="mock", model="mock", local=True)}
    settings.routing = {
        role: "mock" for role in ("architect", "planner", "builder", "reviewer", "debugger")
    }
    save_settings(settings, root)
    database = Database(settings, root)
    database.initialize()
    service = MissionService(root, settings, database)
    service.gateway = gateway  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_a_failed_task_does_not_stop_an_independent_task(git_repo: Path) -> None:
    good_cmd = f"{sys.executable} -c 'pass'"
    bad_cmd = f"{sys.executable} -c 'import sys; sys.exit(1)'"
    gateway = PartialPlanGateway(good_cmd, bad_cmd, dependent=False)
    service = make_partial_service(git_repo, gateway)

    mission, requirements, plan = await service.plan("Two tasks", ProjectMode.SPECIFICATION)
    with pytest.raises(RuntimeError, match="Completed 1 of 2"):
        await service.execute(mission.id, requirements, plan)

    refreshed = service.get(mission.id)
    assert refreshed.status == "failed"
    workspace = Path(refreshed.workspace_path or "")
    # The independent good task ran and committed even though the other failed.
    assert (workspace / "good.py").exists()
    assert "good.py" in git(workspace, "ls-files").splitlines()


class SingleTaskGateway:
    """One task that passes its own check, to isolate the integration gate."""

    def __init__(self, task_cmd: str) -> None:
        self.task_cmd = task_cmd
        self.written = False

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
                problem_statement="Ship one module",
                goals=["Create app.py"],
                functional_requirements=["app.py exists"],
                acceptance_criteria=["app.py exists"],
                test_strategy=[self.task_cmd],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="One task",
                mode=ProjectMode.SPECIFICATION,
                tasks=[
                    TaskSpec(
                        id="only",
                        title="Write the module",
                        objective="create app.py",
                        expected_files=["app.py"],
                        allowed_files=["app.py"],
                        acceptance_criteria=["app.py exists"],
                        verification_commands=[self.task_cmd],
                    )
                ],
            )
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="ok")
        if schema is AgentAction:
            if not self.written:
                self.written = True
                return AgentAction(
                    thought="write", action="write", path="app.py", content="ok = 1\n"
                )
            return AgentAction(thought="done", action="finish", summary="done")
        raise AssertionError(schema)


@pytest.mark.asyncio
async def test_integration_gate_catches_breakage_the_task_checks_missed(git_repo: Path) -> None:
    task_cmd = f"{sys.executable} -c 'pass'"
    integration_cmd = f"{sys.executable} -c 'import sys; sys.exit(2)'"
    gateway = SingleTaskGateway(task_cmd)
    service = make_partial_service(git_repo, gateway)  # type: ignore[arg-type]
    # A whole-project check that the per-task check does not run.
    service.settings.verification.commands = [integration_cmd]

    mission, requirements, plan = await service.plan("Ship one module", ProjectMode.SPECIFICATION)
    with pytest.raises(RuntimeError, match="Integration verification failed"):
        await service.execute(mission.id, requirements, plan)

    refreshed = service.get(mission.id)
    assert refreshed.status == "failed"
    workspace = Path(refreshed.workspace_path or "")
    # The task itself passed and committed; only the assembled-project gate failed.
    assert (workspace / "app.py").exists()
    assert "app.py" in git(workspace, "ls-files").splitlines()


@pytest.mark.asyncio
async def test_a_task_blocked_by_a_failed_dependency_is_skipped(git_repo: Path) -> None:
    good_cmd = f"{sys.executable} -c 'pass'"
    bad_cmd = f"{sys.executable} -c 'import sys; sys.exit(1)'"
    gateway = PartialPlanGateway(good_cmd, bad_cmd, dependent=True)
    service = make_partial_service(git_repo, gateway)

    mission, requirements, plan = await service.plan("Two tasks", ProjectMode.SPECIFICATION)
    with pytest.raises(RuntimeError) as excinfo:
        await service.execute(mission.id, requirements, plan)

    message = str(excinfo.value)
    assert "Completed 0 of 2" in message
    assert "Skipped after a dependency failed" in message
    assert service.get(mission.id).status == "failed"


# ------------------------------- a task too large for the model that must run it


class OversizedTaskGateway:
    """One planned task scoping more files than the executing model can hold.

    Unlike the other harnesses here this one also answers ``capability_envelope``,
    because that is what the mission loop asks before deciding a task will not
    fit. A gateway without it disables sizing entirely — the behaviour every
    other test in this file relies on.
    """

    def __init__(self, command: str, *, files: int = 4, envelope_files: int = 1) -> None:
        self.command = command
        self.files = [f"mod_{index}.py" for index in range(files)]
        self.envelope_files = envelope_files
        self.written: set[str] = set()
        self.builder_turns = 0

    def capability_envelope(self, role: object, *args: object, **kwargs: object) -> object:
        from daino.context import CapabilityEnvelope

        return CapabilityEnvelope(
            profile_name="mock",
            compact=False,
            one_action_per_turn=False,
            max_steps=None,
            working_headroom_tokens=8_000,
            source_tokens=8_000,
            max_files_per_task=self.envelope_files,
            task_source_budget_tokens=8_000,
        )

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
                problem_statement="Create several modules",
                goals=["Create every module"],
                functional_requirements=["every module exists"],
                acceptance_criteria=["every module exists"],
                test_strategy=[self.command],
            )
        if schema is TaskPlan:
            return TaskPlan(
                summary="One large task",
                mode=ProjectMode.SPECIFICATION,
                tasks=[
                    TaskSpec(
                        id="wide",
                        title="Write every module",
                        objective="Create all of the modules",
                        expected_files=list(self.files),
                        allowed_files=list(self.files),
                        acceptance_criteria=["every module exists"],
                        verification_commands=[self.command],
                    )
                ],
            )
        if schema is ReviewReport:
            return ReviewReport(approved=True, summary="ok")
        if schema is AgentAction:
            self.builder_turns += 1
            scope = self._scoped_files(messages)
            for path in scope:
                if path not in self.written:
                    self.written.add(path)
                    return AgentAction(
                        thought="write",
                        action="write",
                        path=path,
                        content=f"value = {path!r}\n",
                    )
            return AgentAction(thought="done", action="finish", summary="done")
        raise AssertionError(schema)

    def _scoped_files(self, messages: object) -> list[str]:
        """The files this slice may touch, read out of its own bundle."""
        for item in messages if isinstance(messages, list) else []:
            if isinstance(item, Message) and item.role == "user":
                try:
                    payload = json.loads(item.content)
                except (ValueError, TypeError):
                    continue
                objective = str(payload.get("task", ""))
                return [path for path in self.files if path in objective]
        return []


def make_sizing_service(root: Path, gateway: object) -> MissionService:
    settings = default_settings(root)
    settings.runtime.default = "local"
    settings.verification.require_review = False
    settings.verification.repair_attempts_local = 0
    settings.verification.total_attempts = 1
    settings.verification.integration_gate = False
    settings.providers = {
        "mock": ProviderConfig(type="vllm", base_url="http://mock.invalid/v1", model="mock")
    }
    settings.models = {"mock": ModelProfileConfig(provider="mock", model="mock", local=True)}
    settings.routing = {
        role: "mock" for role in ("architect", "planner", "builder", "reviewer", "debugger")
    }
    save_settings(settings, root)
    database = Database(settings, root)
    database.initialize()
    service = MissionService(root, settings, database)
    service.gateway = gateway  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_a_task_too_wide_for_the_model_is_split_and_still_completed(
    git_repo: Path,
) -> None:
    """The whole point: the work lands instead of the run dying on a stall."""
    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=4, envelope_files=1)
    service = make_sizing_service(git_repo, gateway)

    mission, requirements, plan = await service.plan("Many modules", ProjectMode.SPECIFICATION)
    refreshed, _ = await service.execute(mission.id, requirements, plan)

    assert refreshed.status == "completed"
    workspace = Path(refreshed.workspace_path or "")
    for path in gateway.files:
        assert (workspace / path).exists(), f"{path} was dropped by the split"


@pytest.mark.asyncio
async def test_the_split_is_persisted_so_a_resume_does_not_redo_the_parent(
    git_repo: Path,
) -> None:
    """Without the database writes, `resume()` rebuilds the oversized parent."""
    from sqlalchemy import select as sa_select

    from daino.persistence.models import Task, TaskDependency

    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=4, envelope_files=1)
    service = make_sizing_service(git_repo, gateway)
    mission, requirements, plan = await service.plan("Many modules", ProjectMode.SPECIFICATION)
    await service.execute(mission.id, requirements, plan)

    with service.database.session() as session:
        rows = {
            row.id: row.status
            for row in session.scalars(sa_select(Task).where(Task.mission_id == mission.id)).all()
        }
        edges = [
            (row.task_id, row.depends_on_id)
            for row in session.scalars(sa_select(TaskDependency)).all()
        ]

    parent = [task_id for task_id, status in rows.items() if status == "cancelled"]
    slices = sorted(task_id for task_id in rows if "-s1-" in task_id)
    assert len(parent) == 1, "the oversized parent must be cancelled, not left pending"
    assert len(slices) == 4
    assert all(rows[task_id] == "completed" for task_id in slices)
    # Sequential, and recorded as such: a resume rebuilds this graph from here.
    assert [(slices[index + 1], slices[index]) for index in range(3)] == [
        edge for edge in edges if edge[0] in slices
    ]
    # And the plan rebuilt from the database skips the cancelled parent.
    _, reloaded = service._load_plan(mission.id)
    assert parent[0] in {task.id for task in reloaded.tasks}


@pytest.mark.asyncio
async def test_intermediate_slices_do_not_commit_before_the_last_one_verifies(
    git_repo: Path,
) -> None:
    """A third of a change has passed no check; it must not enter the history.

    Every slice commits only once the final slice verifies, so the branch never
    holds a state no verification ever ran against.
    """
    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=3, envelope_files=1)
    service = make_sizing_service(git_repo, gateway)
    mission, requirements, plan = await service.plan("Many modules", ProjectMode.SPECIFICATION)

    refreshed, _ = await service.execute(mission.id, requirements, plan)

    workspace = Path(refreshed.workspace_path or "")
    subjects = "\n".join(git(workspace, "log", "--format=%s").splitlines())
    # One commit for the whole change, made by the final slice — not one per
    # slice — and titled with the task the user asked for rather than "(3/3)".
    assert subjects.count("Write every module") == 1
    assert "(3/3)" not in subjects
    tracked = git(workspace, "ls-files").splitlines()
    for path in gateway.files:
        # Every slice's work is in that commit, not just the last one's. This is
        # the part that breaks if the final slice commits only its own paths.
        assert path in tracked, f"{path} was left uncommitted in the working tree"


@pytest.mark.asyncio
async def test_a_task_that_still_will_not_fit_is_not_split_forever(git_repo: Path) -> None:
    """Three independent limits stop the recursion; this asserts the outcome.

    A single file that overruns the whole budget cannot be split between files at
    all, so the loop must give up and report rather than slice indefinitely.
    """
    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=1, envelope_files=1)
    # A budget no single file can fit under.
    gateway.capability_envelope = lambda *args, **kwargs: _pinhole_envelope()  # type: ignore[assignment]
    service = make_sizing_service(git_repo, gateway)
    mission, requirements, plan = await service.plan("One module", ProjectMode.SPECIFICATION)

    refreshed, _ = await service.execute(mission.id, requirements, plan)

    # It runs rather than looping: an unsplittable task is attempted as planned.
    assert refreshed.status in {"completed", "failed"}
    with service.database.session() as session:
        from sqlalchemy import select as sa_select

        from daino.persistence.models import Task

        ids = list(session.scalars(sa_select(Task.id).where(Task.mission_id == mission.id)).all())
    assert not any("-s2-" in task_id for task_id in ids), "split past its generation cap"


def _pinhole_envelope() -> object:
    from daino.context import CapabilityEnvelope

    return CapabilityEnvelope(
        profile_name="mock",
        compact=True,
        one_action_per_turn=True,
        max_steps=None,
        working_headroom_tokens=64,
        source_tokens=64,
        max_files_per_task=1,
        task_source_budget_tokens=1,
    )


@pytest.mark.asyncio
async def test_a_task_that_thrashes_on_compaction_is_split_and_rerun(git_repo: Path) -> None:
    """The field failure, end to end.

    The builder read one file, lost it to compaction, read it again, and the
    no-progress guard ended the run telling the user to rephrase — advice that
    could not have worked, because the wording was never the problem. Here the
    same stop is recognised as a window problem and the task is cut down instead.

    The task fits the envelope, so only the post-flight branch can fire: the
    pre-flight check has nothing to object to.
    """
    from daino.agents.loop import THRASHING_COMPACTIONS, BuilderOutcome, IncompleteRun
    from daino.schemas import Implementation

    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=3, envelope_files=3)
    service = make_sizing_service(git_repo, gateway)
    original = service._run_builder
    stalled: list[str] = []

    async def thrash_once(workspace, spec, context, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not spec.slice_of and spec.id not in stalled:
            stalled.append(spec.id)
            raise IncompleteRun(
                "stopped before finishing",
                BuilderOutcome(
                    implementation=Implementation(summary="stalled", modifications=[]),
                    changed=[],
                    steps=9,
                    completed=False,
                    stop_reason="stall",
                    compactions=THRASHING_COMPACTIONS,
                ),
            )
        return await original(workspace, spec, context, *args, **kwargs)

    service._run_builder = thrash_once  # type: ignore[assignment]
    mission, requirements, plan = await service.plan("Many modules", ProjectMode.SPECIFICATION)

    refreshed, _ = await service.execute(mission.id, requirements, plan)

    assert stalled, "the stall never happened, so this test proved nothing"
    assert refreshed.status == "completed"
    workspace = Path(refreshed.workspace_path or "")
    for path in gateway.files:
        assert (workspace / path).exists()


@pytest.mark.asyncio
async def test_a_permanently_thrashing_model_stops_rather_than_splitting_forever(
    git_repo: Path,
) -> None:
    """Bounded: two generations of splitting, then the honest failure message."""
    from daino.agents.loop import THRASHING_COMPACTIONS, BuilderOutcome, IncompleteRun
    from daino.schemas import Implementation

    gateway = OversizedTaskGateway(f"{sys.executable} -c 'pass'", files=4, envelope_files=4)
    service = make_sizing_service(git_repo, gateway)
    attempts: list[str] = []

    async def always_thrash(workspace, spec, context, *args, **kwargs):  # type: ignore[no-untyped-def]
        attempts.append(spec.id)
        raise IncompleteRun(
            "stopped before finishing",
            BuilderOutcome(
                implementation=Implementation(summary="stalled", modifications=[]),
                changed=[],
                steps=9,
                completed=False,
                stop_reason="stall",
                compactions=THRASHING_COMPACTIONS + 2,
            ),
        )

    service._run_builder = always_thrash  # type: ignore[assignment]
    mission, requirements, plan = await service.plan("Many modules", ProjectMode.SPECIFICATION)

    with pytest.raises(RuntimeError, match="Completed 0 of"):
        await service.execute(mission.id, requirements, plan)

    # It terminates, and does so after a bounded number of builder invocations
    # rather than slicing until the ids run out of characters.
    assert len(attempts) < 20, attempts
    assert not any(task_id.count("-s") > 2 for task_id in attempts)
