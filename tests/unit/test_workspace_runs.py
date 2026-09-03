"""Executing a workspace plan: one task per turn, steerable, and interruptible.

The agent itself is stubbed here. What is under test is the executor's contract
— which task runs next, what a failure does to the rest of the plan, what
survives a pause, and what a person can change while it is working — none of
which should depend on a model being reachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from daino.application.context import ProjectContext
from daino.application.workspace_run_service import (
    MAX_CONSECUTIVE_FAILURES,
    RunError,
    WorkspaceRunApplicationService,
)
from daino.config import default_settings, save_settings
from daino.events import EventBus, WorkspaceRunUpdated
from daino.persistence import Database
from daino.persistence.models import ConversationSession
from daino.schemas import ChatOutcome
from daino.workbench.service import WorkbenchService


class FakeMissions:
    """Stands in for the agent: records each turn and returns a canned result.

    Sessions are created for real, because the executor attaches the workspace
    to one and the attachment is checked against the database.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.turns: list[str] = []
        #: Instruction substring -> the exception to raise for that turn.
        self.failures: dict[str, str] = {}
        #: Instruction substrings whose turn stops on a compaction stall.
        self.stalls: set[str] = set()
        #: Called with (instruction, approve, approve_action) before answering.
        self.hook: Any = None
        self.sessions: list[str] = []
        self.messages: list[tuple[str, str]] = []

    def create_session(self, title: str = "") -> str:
        self.sessions.append(title)
        identifier = f"session-{len(self.sessions)}"
        with self.database.session() as session:
            session.add(
                ConversationSession(
                    id=identifier,
                    project_id=self.database.project().id,
                    title=title[:255],
                )
            )
        return identifier

    def add_message(self, session_id: str, **fields: Any) -> None:
        self.messages.append((session_id, str(fields.get("content", ""))))

    async def chat(
        self,
        instruction: str,
        session_id: str,
        *,
        profile_override: str = "",
        approve: Any = None,
        approve_action: Any = None,
    ) -> ChatOutcome:
        self.turns.append(instruction)
        for marker, error in self.failures.items():
            if marker in instruction:
                raise RuntimeError(error)
        for marker in self.stalls:
            if marker in instruction:
                raise _thrashing_run()
        if self.hook is not None:
            await self.hook(instruction, approve, approve_action)
        return ChatOutcome(mission_id="m-1", answer=f"Did: {instruction[:40]}")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[ProjectContext]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    context = ProjectContext(root=tmp_path, settings=settings, database=database, events=EventBus())
    yield context
    database.engine.dispose()


@pytest.fixture
def workbench(project: ProjectContext) -> WorkbenchService:
    return WorkbenchService(project.root, project.database, events=project.events)


@pytest.fixture
def missions(project: ProjectContext) -> FakeMissions:
    return FakeMissions(project.database)


@pytest.fixture
def runs(
    project: ProjectContext, missions: FakeMissions, workbench: WorkbenchService
) -> WorkspaceRunApplicationService:
    return WorkspaceRunApplicationService(project, missions, workbench)  # type: ignore[arg-type]


async def _drain(service: WorkspaceRunApplicationService, run_id: str) -> None:
    """Wait for the executor task to finish, whatever it decided."""
    control = service._controls.get(run_id)
    task = control.task if control else None
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def test_a_run_works_the_plan_to_completion(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """One turn per step, in order, each marked as it goes."""
    workspace = workbench.create("Competitor analysis", goal="Recommend a vendor")
    workbench.set_tasks(workspace.id, ["Research three vendors", "Compare them", "Recommend one"])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    finished = runs.get(run.id)
    assert finished.status == "completed"
    assert [task.status for task in workbench.get(workspace.id).tasks] == ["completed"] * 3
    assert len(missions.turns) == 3
    # Each turn is told which step it is on and what the run is for.
    assert "step 1 of 3" in missions.turns[0]
    assert "Research three vendors" in missions.turns[0]
    assert "Recommend a vendor" in missions.turns[0]
    # And the second turn can see what the first one concluded.
    assert "Already done in this plan" in missions.turns[1]


async def test_the_plan_is_worked_in_order_with_dependencies_respected(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """Order covers most plans; ``depends_on`` covers the rest."""
    workspace = workbench.create("Proposal")
    tasks = workbench.set_tasks(workspace.id, ["Draft", "Research"])
    # The draft cannot happen until the research does, whatever the order says.
    workbench.update_task(workspace.id, tasks[0].id, depends_on=[tasks[1].id])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    assert "Research" in missions.turns[0]
    assert "Draft" in missions.turns[1]


async def test_a_failed_step_holds_the_run_and_keeps_what_finished(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """The run asks rather than writing a conclusion on missing evidence."""
    workspace = workbench.create("Analysis")
    workbench.set_tasks(workspace.id, ["Gather data", "Analyse it", "Write it up"])
    missions.failures["Analyse it"] = "The spreadsheet could not be read"

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    held = runs.get(run.id)
    assert held.status == "waiting_for_user"
    assert "Analyse it" in held.error
    statuses = [task.status for task in workbench.get(workspace.id).tasks]
    assert statuses == ["completed", "failed", "pending"]
    failed = workbench.get(workspace.id).tasks[1]
    assert "spreadsheet could not be read" in failed.error
    # The step after the failure was never attempted.
    assert not any("Write it up" in turn for turn in missions.turns)


async def test_a_failed_step_can_be_retried_or_skipped(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """Recovery keeps the run; it does not start the plan over."""
    workspace = workbench.create("Analysis")
    workbench.set_tasks(workspace.id, ["Gather data", "Analyse it", "Write it up"])
    missions.failures["Analyse it"] = "No parser installed"

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    # The user fixes the cause and retries: the step runs again, and the rest
    # of the plan continues behind it.
    missions.failures.clear()
    failed = workbench.get(workspace.id).tasks[1]
    workbench.update_task(workspace.id, failed.id, status="pending", error="")
    await runs.resume(run.id)
    await _drain(runs, run.id)

    assert runs.get(run.id).status == "completed"
    assert [task.status for task in workbench.get(workspace.id).tasks] == ["completed"] * 3
    # "Gather data" was worked once, not twice: finished work is not redone.
    assert sum("This step: Gather data" in turn for turn in missions.turns) == 1


async def test_pause_stops_after_the_current_step_and_resume_continues(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """A step is the atomic unit — pausing never interrupts one mid-turn."""
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["One", "Two", "Three"])

    async def pause_during_first_turn(instruction: str, *_: Any) -> None:
        if "This step: One" in instruction:
            runs.pause(active_run.id)

    missions.hook = pause_during_first_turn
    active_run = await runs.start(workspace.id)
    await _drain(runs, active_run.id)

    paused = runs.get(active_run.id)
    assert paused.status == "paused"
    # The step that was running when Pause arrived still finished.
    assert workbench.get(workspace.id).tasks[0].status == "completed"
    assert workbench.get(workspace.id).tasks[1].status == "pending"

    missions.hook = None
    await runs.resume(active_run.id)
    await _drain(runs, active_run.id)

    assert runs.get(active_run.id).status == "completed"
    assert sum("This step: One" in turn for turn in missions.turns) == 1


async def test_stopping_keeps_the_plan_and_everything_already_produced(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService
) -> None:
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["One", "Two"])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)
    stopped = runs.stop(run.id)

    assert stopped.status == "cancelled"
    # The plan itself is untouched — Stop cancels the run, not the work.
    assert len(workbench.get(workspace.id).tasks) == 2


async def test_steering_updates_the_plan_before_the_next_step(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """New direction is folded in at the boundary, not mid-step."""
    workspace = workbench.create("Competitors")
    workbench.set_tasks(workspace.id, ["Research", "Compare"])

    async def steer_during_first_turn(instruction: str, *_: Any) -> None:
        if "This step: Research" in instruction:
            runs.steer(active_run.id, "Also compare enterprise pricing.")

    missions.hook = steer_during_first_turn
    active_run = await runs.start(workspace.id)
    await _drain(runs, active_run.id)

    steering = [
        turn for turn in missions.turns if "new direction while the plan was running" in turn
    ]
    assert len(steering) == 1
    assert "Also compare enterprise pricing." in steering[0]
    # It lands between steps: after the first finished, before the second began.
    assert missions.turns.index(steering[0]) == 1
    assert runs.get(active_run.id).status == "completed"
    assert [task.status for task in workbench.get(workspace.id).tasks] == ["completed"] * 2


async def test_an_action_needing_approval_holds_the_run_until_answered(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """Deleting is asked about; writing inside the workspace is not."""
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["Tidy up"])
    decisions: list[bool] = []

    async def try_two_actions(instruction: str, _approve: Any, approve_action: Any) -> None:
        # A write inside the workspace proceeds without anyone being asked.
        decisions.append(await approve_action("write", {"path": f"{workspace.folder}/findings.md"}))
        # A delete does not: the run parks at waiting_for_approval until the
        # user answers, and only then continues.
        pending = asyncio.create_task(
            approve_action("delete_file", {"path": f"{workspace.folder}/old.md"})
        )
        await _until(lambda: runs.get(active_run.id).status == "waiting_for_approval")
        approval = runs.get(active_run.id).pending_approval
        assert approval is not None and "old.md" in approval.action
        runs.resolve_approval(active_run.id, approval.id, True)
        decisions.append(await pending)

    missions.hook = try_two_actions
    active_run = await runs.start(workspace.id)
    await _drain(runs, active_run.id)

    assert decisions == [True, True]
    finished = runs.get(active_run.id)
    assert finished.status == "completed"
    assert finished.pending_approval is None
    assert any(step.kind == "approval" for step in finished.steps)


async def test_a_denied_action_is_refused_without_failing_the_step(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["Tidy up"])
    allowed: list[bool] = []

    async def ask_and_be_denied(instruction: str, _approve: Any, approve_action: Any) -> None:
        pending = asyncio.create_task(approve_action("delete_file", {"path": "notes.md"}))
        await _until(lambda: runs.get(active_run.id).status == "waiting_for_approval")
        approval = runs.get(active_run.id).pending_approval
        assert approval is not None
        runs.resolve_approval(active_run.id, approval.id, False)
        allowed.append(await pending)

    missions.hook = ask_and_be_denied
    active_run = await runs.start(workspace.id)
    await _drain(runs, active_run.id)

    assert allowed == [False]
    assert runs.get(active_run.id).status == "completed"


async def test_a_run_publishes_progress_for_the_browser(
    runs: WorkspaceRunApplicationService,
    workbench: WorkbenchService,
    project: ProjectContext,
) -> None:
    seen: list[WorkspaceRunUpdated] = []
    project.events.subscribe(
        lambda event: seen.append(event) if isinstance(event, WorkspaceRunUpdated) else None
    )
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["One"])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    assert [event.status for event in seen][-1] == "completed"
    assert any(event.message == "One" for event in seen)


async def test_a_run_interrupted_by_a_restart_comes_back_as_paused(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService
) -> None:
    """The row says running; no executor is behind it. Resume is the honest offer."""
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["One", "Two"])
    run = await runs.start(workspace.id)
    await _drain(runs, run.id)
    runs.runs.update(run.id, status="running")

    recovered = runs.reconcile()

    assert run.id in recovered
    revived = runs.get(run.id)
    assert revived.status == "paused"
    assert "Interrupted" in revived.error


async def test_a_plan_with_nothing_pending_is_not_run(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService
) -> None:
    workspace = workbench.create("Report")
    tasks = workbench.set_tasks(workspace.id, ["One"])
    workbench.update_task(workspace.id, tasks[0].id, status="completed")

    with pytest.raises(RunError, match="already done"):
        await runs.start(workspace.id)


async def test_the_run_records_a_timeline_that_survives_the_event_bus(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService
) -> None:
    """A user who comes back tomorrow reads this, not the socket."""
    workspace = workbench.create("Report")
    workbench.set_tasks(workspace.id, ["One", "Two"])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    kinds = [step.kind for step in runs.get(run.id).steps]
    assert kinds[0] == "run_started"
    assert kinds.count("task_started") == 2
    assert kinds.count("task_completed") == 2
    assert kinds[-1] == "run_finished"


async def test_a_skill_is_chosen_from_the_goal_and_shown_in_the_run(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    workspace = workbench.create("Competitors", goal="Research three competitors and recommend one")
    workbench.set_tasks(workspace.id, ["Research"])

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    assert runs.get(run.id).skill == "competitive-research"
    # And the skill's instructions reach the turn that does the work.
    assert "Competitive Research" in missions.turns[0]


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    """Wait for a condition the executor reaches on another task."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Condition was never reached")
        await asyncio.sleep(0.01)


async def test_a_plan_that_keeps_failing_the_same_step_eventually_gives_up(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """The consecutive-failure guard has to survive the retry that triggers it.

    The counter used to live in the executor's stack frame, and a failure
    settles the run and returns — so every retry started a new invocation with
    the count back at zero and the limit could never be reached. A plan that
    does not work would ask forever instead of stopping.
    """
    workspace = workbench.create("Analysis")
    tasks = workbench.set_tasks(workspace.id, ["Read the file", "Write it up"])
    missions.failures["Read the file"] = "No such file"

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)
    assert runs.get(run.id).status == "waiting_for_user"

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        workbench.update_task(workspace.id, tasks[0].id, status="pending", error="")
        await runs.resume(run.id)
        await _drain(runs, run.id)

    stopped = runs.get(run.id)
    assert stopped.status == "failed"
    assert f"{MAX_CONSECUTIVE_FAILURES} steps in a row" in stopped.error


async def test_deciding_to_skip_a_step_clears_the_failure_streak(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """The guard stops an unattended loop, not a person steering one."""
    workspace = workbench.create("Analysis")
    workbench.set_tasks(workspace.id, ["Read the file", "Write it up"])
    missions.failures["Read the file"] = "No such file"

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)
    assert runs.get(run.id).metadata["consecutive_failures"] == 1

    runs.clear_failure_streak(run.id)

    assert "consecutive_failures" not in runs.get(run.id).metadata


async def test_a_restart_while_waiting_for_approval_leaves_a_resumable_run(
    runs: WorkspaceRunApplicationService,
    workbench: WorkbenchService,
    missions: FakeMissions,
    project: ProjectContext,
) -> None:
    """The approval future lived in memory, so a restart has to release the run.

    Left as-is, the run sat at ``waiting_for_approval`` on a prompt nothing
    could answer, and the step it was on stayed ``in_progress`` — invisible to
    the executor, so resuming stepped over it and could call the run complete
    with the work never done.
    """
    workspace = workbench.create("Analysis")
    tasks = workbench.set_tasks(workspace.id, ["Read the file", "Write it up"])

    # Reproduce exactly the state the process died in.
    run = await runs.start(workspace.id)
    await _drain(runs, run.id)
    workbench.update_task(workspace.id, tasks[0].id, status="in_progress")
    runs.runs.update(
        run.id,
        status="waiting_for_approval",
        current_task_id=tasks[0].id,
        metadata={
            "pending_approval": {
                "id": "wsapp-1",
                "action": "rm -rf build",
                "reason": "writes outside the workspace",
                "level": "local_execution",
            }
        },
    )

    # A fresh process reconciles what it finds.
    recovered = WorkspaceRunApplicationService(
        project,
        missions,
        workbench,  # type: ignore[arg-type]
    ).reconcile()

    assert run.id in recovered
    after = runs.get(run.id)
    assert after.status == "paused"
    assert after.pending_approval is None
    assert "not granted" in after.error
    # The interrupted step is back in the queue rather than stranded.
    assert workbench.get(workspace.id).tasks[0].status == "pending"


def _thrashing_run() -> Exception:
    """A turn that stopped because it kept losing its context, not because it was stuck."""
    from daino.agents.loop import THRASHING_COMPACTIONS, BuilderOutcome, IncompleteRun
    from daino.schemas import Implementation

    return IncompleteRun(
        "The coding agent stopped before finishing.",
        BuilderOutcome(
            implementation=Implementation(summary="stalled", modifications=[]),
            changed=[],
            steps=9,
            completed=False,
            stop_reason="stall",
            compactions=THRASHING_COMPACTIONS + 2,
        ),
    )


async def test_a_step_too_large_for_the_model_says_so_rather_than_just_failing(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """A workspace plan is prose, so nothing can split it automatically.

    What the run can do is name the cause and the fix. "The agent stopped before
    finishing" tells the user nothing they can act on; "this step was too large,
    split it" tells them exactly what to change before pressing Retry.
    """
    workspace = workbench.create("Analysis")
    workbench.set_tasks(workspace.id, ["Gather data", "Write the whole report", "Review it"])
    missions.stalls.add("Write the whole report")

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    held = runs.get(run.id)
    assert held.status == "waiting_for_user"
    failed = workbench.get(workspace.id).tasks[1]
    assert "too large for the model" in failed.error
    assert "Split this step into smaller ones" in failed.error
    # And the plan is intact, so the user can edit it and retry.
    assert [task.status for task in workbench.get(workspace.id).tasks] == [
        "completed",
        "failed",
        "pending",
    ]


async def test_an_ordinary_failure_is_not_reported_as_a_size_problem(
    runs: WorkspaceRunApplicationService, workbench: WorkbenchService, missions: FakeMissions
) -> None:
    """Wrong advice is worse than generic advice."""
    workspace = workbench.create("Analysis")
    workbench.set_tasks(workspace.id, ["Gather data", "Analyse it"])
    missions.failures["Analyse it"] = "The spreadsheet could not be read"

    run = await runs.start(workspace.id)
    await _drain(runs, run.id)

    failed = workbench.get(workspace.id).tasks[1]
    assert "too large for the model" not in failed.error
