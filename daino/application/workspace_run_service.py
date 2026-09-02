"""Executing a workspace plan: one step at a time, with a way to intervene.

The Workspace tab could already hold a plan; this is what makes the plan run.
The shape is deliberately unglamorous — a loop that picks the next eligible
task, runs *one ordinary chat turn* for it, records what happened, and moves on.

Two decisions are worth stating, because they are what keep this reliable:

* **One task, one turn.** The alternative — a single prompt asking the model to
  carry out seven steps — produces a plausible narrative of work rather than the
  work, and there is no point at which a person can steer it. A turn per task
  reuses the whole existing apparatus: the workspace tool surface, the system
  prompt, source recording, revision history, changesets, and approvals. The
  executor adds no agent machinery of its own.
* **A failure stops the run and asks.** Skipping past a failed research step and
  writing the recommendation anyway is precisely the confident-but-baseless
  output this codebase refuses everywhere else. The run holds at
  ``waiting_for_user`` with the reason attached, and Retry / Skip is the user's
  call. Everything already finished stays finished.

Runs are single-agent on purpose. The loop is written so a future parallel
executor could take the same eligible-task computation, but nothing here
pretends to be an orchestrator of workers.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from daino.application.context import ProjectContext
from daino.application.mission_service import MissionApplicationService
from daino.events import WorkspaceRunUpdated
from daino.exceptions import DainoError
from daino.utils.ids import new_id
from daino.workbench.approvals import ApprovalPolicy
from daino.workbench.changes import ChangeSetStore
from daino.workbench.models import PendingApproval, Workspace, WorkspaceRun, WorkspaceTask
from daino.workbench.runs import RunStore
from daino.workbench.service import WorkbenchError, WorkbenchService
from daino.workbench.skills import SkillLoader

#: A run that fails this many steps in a row stops asking and gives up. One
#: failure is a question for the user; four is a plan that does not work.
MAX_CONSECUTIVE_FAILURES = 3

#: How many finished steps are quoted back to the model as context for the next
#: one. Enough to carry the thread, bounded so a long plan does not grow the
#: prompt without limit — the artifacts themselves are what carry the detail.
RECENT_STEPS = 6


class RunError(DainoError):
    """Raised when a run cannot be started, resumed, or steered."""


@dataclass
class _Control:
    """The live handle on one executing run.

    Held in memory only: everything that must survive a restart is in the run
    row, and a control without a process behind it would be a lie.
    """

    task: asyncio.Task[None] | None = None
    pause_requested: bool = False
    stop_requested: bool = False
    #: Instructions the user sent while the run was working, applied at the next
    #: step boundary rather than mid-task.
    steering: list[str] = field(default_factory=list)
    #: Approval id -> the future the executor is blocked on.
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)

    def abandon(self) -> None:
        for future in self.approvals.values():
            if not future.done():
                future.set_result(False)
        self.approvals.clear()


class WorkspaceRunApplicationService:
    """Start, steer, and stop the execution of a workspace plan."""

    def __init__(
        self,
        context: ProjectContext,
        missions: MissionApplicationService,
        workbench: WorkbenchService,
        *,
        turn_lock: asyncio.Lock | None = None,
    ) -> None:
        self.context = context
        self.missions = missions
        self.workbench = workbench
        self.runs = RunStore(context.database)
        self.changes = ChangeSetStore(context.database, workbench)
        self.skills = SkillLoader(context.root)
        #: The project-wide turn lock. One working tree, one runtime: a run's
        #: turns must not interleave with a turn the user started in CODE.
        self.turn_lock = turn_lock or asyncio.Lock()
        self._controls: dict[str, _Control] = {}

    # ------------------------------------------------------------- lifecycle

    def reconcile(self) -> list[str]:
        """Mark runs the previous process died holding as paused."""
        return self.runs.reconcile()

    def active(self, workspace_id: str) -> WorkspaceRun | None:
        return self.runs.active_for(workspace_id)

    def latest(self, workspace_id: str) -> WorkspaceRun | None:
        return self.runs.latest_for(workspace_id)

    def active_for_session(self, session_id: str) -> WorkspaceRun | None:
        """The run attached to this conversation, if one is executing.

        What makes steering work: the agent panel is shared, so a message typed
        while a plan is running has to be recognised as direction for the plan
        rather than rejected as "a turn is already in progress".
        """
        workspace_id = self.workbench.workspace_for_session(session_id)
        if not workspace_id:
            return None
        return self.runs.active_for(workspace_id)

    def get(self, run_id: str) -> WorkspaceRun:
        run = self.runs.get(run_id)
        if run is None:
            raise RunError(f"Unknown run {run_id}")
        return run

    async def start(
        self, workspace_id: str, *, goal: str = "", profile: str = "", skill: str = ""
    ) -> WorkspaceRun:
        """Begin executing this workspace's plan."""
        workspace = self._workspace(workspace_id)
        existing = self.runs.active_for(workspace_id)
        if existing is not None and self._running(existing.id):
            raise RunError("A run is already in progress for this workspace.")
        if not any(task.status == "pending" for task in workspace.tasks):
            raise RunError(
                "Every step of the plan is already done or failed. Add a step, or "
                "reopen one, before running the plan."
            )
        chosen = skill or self.skills.select(goal or workspace.goal, workspace.kind)
        run = self.runs.create(
            workspace_id,
            goal=goal or workspace.goal,
            skill=chosen,
            profile=profile,
        )
        self.runs.add_step(
            run.id,
            "run_started",
            f"Running the plan: {run.goal or workspace.name}",
            detail={"skill": chosen},
        )
        return self._spawn(run.id)

    async def resume(self, run_id: str) -> WorkspaceRun:
        run = self.get(run_id)
        if self._running(run_id):
            return run
        if run.status in {"completed", "cancelled"}:
            raise RunError("This run has finished. Start a new one to keep going.")
        control = self._controls.setdefault(run_id, _Control())
        control.pause_requested = False
        control.stop_requested = False
        self.runs.add_step(run_id, "note", "Resumed by the user.")
        return self._spawn(run_id)

    def pause(self, run_id: str) -> WorkspaceRun:
        """Stop after the current step, rather than in the middle of one.

        A task is the atomic unit here: interrupting one mid-turn would leave a
        half-written document and a task marked in progress that nothing owns.
        """
        run = self.get(run_id)
        control = self._controls.setdefault(run_id, _Control())
        control.pause_requested = True
        if not self._running(run_id):
            return self._settle(run_id, "paused", "Paused.")
        self._note(run, "Pausing after the current step…")
        return self.get(run_id)

    def stop(self, run_id: str) -> WorkspaceRun:
        """Cancel the run, keeping the plan and everything already produced."""
        run = self.get(run_id)
        control = self._controls.setdefault(run_id, _Control())
        control.stop_requested = True
        control.abandon()
        if control.task is not None and not control.task.done():
            control.task.cancel()
        if run.current_task_id:
            # An interrupted step goes back to pending: it was not done, and
            # leaving it "in progress" would strand it forever.
            with contextlib.suppress(WorkbenchError):
                self.workbench.update_task(run.workspace_id, run.current_task_id, status="pending")
        return self._settle(run_id, "cancelled", "Stopped by the user.")

    def steer(self, run_id: str, instruction: str) -> WorkspaceRun:
        """Take new direction from the user without discarding finished work."""
        text = instruction.strip()
        if not text:
            raise RunError("Nothing to steer with.")
        run = self.get(run_id)
        if not run.active:
            raise RunError("This run has finished; send it as a new message instead.")
        control = self._controls.setdefault(run_id, _Control())
        control.steering.append(text)
        self.runs.add_step(run_id, "steer", text)
        self._publish(run, message="Plan updated from your instruction")
        if run.status == "waiting_for_user":
            # The user answering is what unblocks a run that stopped to ask.
            self.runs.update(run_id, status="running", error="")
            self._spawn(run_id)
        return self.get(run_id)

    # ------------------------------------------------------------- approvals

    def resolve_approval(self, run_id: str, approval_id: str, approved: bool) -> WorkspaceRun:
        control = self._controls.get(run_id)
        future = (control.approvals if control else {}).get(approval_id)
        if future is None or future.done():
            raise RunError("That approval is no longer waiting for an answer.")
        future.set_result(approved)
        return self.get(run_id)

    def _approval_callback(self, run_id: str) -> Any:
        """The command/network approver handed to each of the run's turns."""

        async def approve(command: str, reason: str) -> tuple[bool, bool]:
            granted = await self._ask(
                run_id, action=command, reason=reason, level="local_execution"
            )
            # Never "remember": a run makes many turns, and a remembered yes
            # inside an unattended loop is how one approval becomes twenty.
            return granted, False

        return approve

    def _action_callback(self, run_id: str, policy: ApprovalPolicy, folder: str) -> Any:
        """The per-action gate: classify, and ask only when it matters."""

        async def gate(action: str, arguments: dict[str, Any]) -> bool:
            enriched = {**arguments, "__workspace_folder": folder}
            if not policy.needs_approval(action, enriched):
                return True
            return await self._ask(
                run_id,
                action=policy.describe(action, arguments),
                reason=policy.reason(action, enriched),
                level=policy.level_for(action, enriched).value,
            )

        return gate

    async def _ask(self, run_id: str, *, action: str, reason: str, level: str) -> bool:
        """Hold the run at ``waiting_for_approval`` until a person answers."""
        control = self._controls.setdefault(run_id, _Control())
        approval = PendingApproval(id=new_id("wsapp"), action=action, reason=reason, level=level)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        control.approvals[approval.id] = future
        run = self.get(run_id)
        metadata = {**run.metadata, "pending_approval": approval.model_dump(mode="json")}
        self.runs.update(run_id, status="waiting_for_approval", metadata=metadata)
        self.runs.add_step(run_id, "approval", f"Waiting for approval: {action}", detail={
            "approval_id": approval.id,
            "reason": reason,
            "level": level,
        })
        self._publish(self.get(run_id), message=f"Needs approval: {action}")
        try:
            approved = await future
        finally:
            control.approvals.pop(approval.id, None)
            current = self.get(run_id)
            metadata = {key: value for key, value in current.metadata.items()
                        if key != "pending_approval"}
            self.runs.update(run_id, status="running", metadata=metadata)
        self.runs.add_step(
            run_id,
            "approval",
            f"{'Allowed' if approved else 'Denied'}: {action}",
            detail={"approved": approved},
        )
        self._publish(self.get(run_id))
        return approved

    # -------------------------------------------------------------- executor

    def _spawn(self, run_id: str) -> WorkspaceRun:
        control = self._controls.setdefault(run_id, _Control())
        run = self.runs.update(run_id, status="running", error="")
        assert run is not None
        self._publish(run, message="Run started")
        control.task = asyncio.create_task(self._execute(run_id))
        return run

    def _running(self, run_id: str) -> bool:
        control = self._controls.get(run_id)
        return bool(control and control.task is not None and not control.task.done())

    async def _execute(self, run_id: str) -> None:
        """Work the plan until it is done, blocked, paused, or stopped."""
        control = self._controls.setdefault(run_id, _Control())
        failures = 0
        try:
            while True:
                run = self.get(run_id)
                if control.stop_requested:
                    return
                if control.pause_requested:
                    self._settle(run_id, "paused", "Paused after the current step.")
                    return
                if control.steering:
                    await self._apply_steering(run_id, control)
                    continue

                workspace = self._workspace(run.workspace_id)
                task = _next_task(workspace.tasks)
                if task is None:
                    if _blocked(workspace.tasks):
                        self._settle(
                            run_id,
                            "waiting_for_user",
                            "Some steps are waiting on steps that did not finish.",
                        )
                        return
                    self._complete(run_id, workspace)
                    return

                ok = await self._run_task(run_id, workspace, task)
                failures = 0 if ok else failures + 1
                if not ok:
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._settle(
                            run_id,
                            "failed",
                            f"Stopped after {failures} steps in a row failed.",
                        )
                        return
                    self._settle(
                        run_id,
                        "waiting_for_user",
                        f"'{task.content}' did not finish. Retry it, skip it, or tell "
                        "Daino what to do differently.",
                    )
                    return
        except asyncio.CancelledError:
            self._settle(run_id, "cancelled", "Stopped by the user.")
            raise
        except Exception as exc:  # noqa: BLE001 - a run must report, never vanish
            self._settle(run_id, "failed", f"The run stopped: {exc}")

    async def _run_task(
        self, run_id: str, workspace: Workspace, task: WorkspaceTask
    ) -> bool:
        """Execute one plan step as a single ordinary chat turn."""
        run = self.get(run_id)
        self.workbench.update_task(workspace.id, task.id, status="in_progress")
        self.runs.update(run_id, current_task_id=task.id)
        self.runs.add_step(run_id, "task_started", task.content, task_id=task.id)
        self._publish(self.get(run_id), task_id=task.id, message=task.content)

        session_id = self._session(workspace)
        instruction = self._instruction(run, workspace, task)
        policy = ApprovalPolicy(self.context.settings)
        # Taken before the turn so the change set describes exactly this step,
        # even when the same document is edited again by the next one.
        before = self.changes.snapshot(workspace.id)
        try:
            async with self._turn():
                outcome = await self.missions.chat(
                    instruction,
                    session_id,
                    profile_override=run.profile,
                    approve=self._approval_callback(run_id),
                    approve_action=self._action_callback(run_id, policy, workspace.folder),
                )
        except asyncio.CancelledError:
            self.workbench.update_task(workspace.id, task.id, status="pending")
            raise
        except Exception as exc:  # noqa: BLE001 - the failure is the result here
            reason = str(exc).strip() or exc.__class__.__name__
            # A step that failed may still have written something. Grouping it
            # is what lets the user undo a half-finished document in one act
            # instead of hunting through per-file history for it.
            self.changes.record(
                workspace.id,
                before=before,
                run_id=run_id,
                task_id=task.id,
                summary=f"Left behind by a failed step: {reason}"[:2_000],
            )
            self._fail_task(workspace.id, task, reason)
            self.runs.add_step(run_id, "task_failed", f"{task.content} — {reason}", task_id=task.id)
            self._publish(self.get(run_id), task_id=task.id, message=f"Step failed: {reason}")
            return False

        summary = (outcome.answer or outcome.summary or "").strip()
        touched = sorted({diff.path for diff in outcome.diffs})
        change = self.changes.record(
            workspace.id, before=before, run_id=run_id, task_id=task.id, summary=summary
        )
        self.workbench.update_task(
            workspace.id,
            task.id,
            status="completed",
            notes=summary[:4_000],
            artifact_path=_workspace_artifact(touched, workspace.folder),
        )
        for path in touched:
            self.runs.add_step(run_id, "artifact", path, task_id=task.id, detail={"path": path})
        self.runs.add_step(
            run_id,
            "task_completed",
            task.content,
            task_id=task.id,
            detail={
                "summary": summary[:2_000],
                "artifacts": touched,
                "change_set_id": change.id if change else "",
            },
        )
        self._publish(self.get(run_id), task_id=task.id, message=f"Finished: {task.content}")
        return True

    async def _apply_steering(self, run_id: str, control: _Control) -> None:
        """Fold the user's new direction into the plan before the next step."""
        pending, control.steering = list(control.steering), []
        run = self.get(run_id)
        workspace = self._workspace(run.workspace_id)
        instruction = _STEER_TEMPLATE.format(
            direction="\n".join(f"- {item}" for item in pending),
            plan=_plan_text(workspace.tasks),
        )
        try:
            async with self._turn():
                await self.missions.chat(
                    instruction,
                    self._session(workspace),
                    profile_override=run.profile,
                    approve=self._approval_callback(run_id),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - steering must not kill the run
            self.runs.add_step(run_id, "note", f"Could not update the plan: {exc}")
            return
        self.runs.add_step(run_id, "note", "Plan updated from your instruction.")
        self._publish(self.get(run_id), message="Plan updated from your instruction")

    # ---------------------------------------------------------------- pieces

    @contextlib.asynccontextmanager
    async def _turn(self) -> Any:
        """One turn at a time across the whole project, run or not."""
        await self.turn_lock.acquire()
        try:
            yield
        finally:
            self.turn_lock.release()

    def _instruction(self, run: WorkspaceRun, workspace: Workspace, task: WorkspaceTask) -> str:
        done = [item for item in workspace.tasks if item.status == "completed"][-RECENT_STEPS:]
        finished = "\n".join(
            f"- {item.content}" + (f" — {_one_line(item.notes)}" if item.notes else "")
            for item in done
        )
        skill = self.skills.get(run.skill) if run.skill else None
        parts = [
            _TASK_TEMPLATE.format(
                position=_position(workspace.tasks, task),
                total=len(workspace.tasks),
                goal=run.goal or workspace.goal or workspace.name,
                task=task.content,
            )
        ]
        if finished:
            parts.append(f"Already done in this plan:\n{finished}")
        if task.notes:
            parts.append(f"Notes on this step:\n{task.notes}")
        if task.error:
            parts.append(f"The previous attempt failed with: {task.error}\nTry a different way.")
        if skill is not None:
            parts.append(skill.as_prompt())
        parts.append(_TASK_CONTRACT)
        return "\n\n".join(parts)

    def _session(self, workspace: Workspace) -> str:
        """The workspace's own conversation, created on first use."""
        if workspace.session_id:
            return workspace.session_id
        session_id = self.missions.create_session(workspace.name)
        self.workbench.attach_session(workspace.id, session_id)
        return session_id

    def _workspace(self, workspace_id: str) -> Workspace:
        try:
            return self.workbench.get(workspace_id)
        except WorkbenchError as exc:
            raise RunError(str(exc)) from exc

    def _fail_task(self, workspace_id: str, task: WorkspaceTask, reason: str) -> None:
        with contextlib.suppress(WorkbenchError):
            self.workbench.update_task(
                workspace_id, task.id, status="failed", error=reason[:2_000]
            )

    def _complete(self, run_id: str, workspace: Workspace) -> None:
        """Finish the run and leave a summary of what it produced."""
        run = self.get(run_id)
        produced = sorted(
            {
                step.detail.get("path", "")
                for step in run.steps
                if step.kind == "artifact" and step.detail.get("path")
            }
        )
        done = sum(task.status == "completed" for task in workspace.tasks)
        metadata = {**run.metadata, "artifacts": produced, "sources": len(workspace.sources)}
        self.runs.update(run_id, status="completed", current_task_id="", metadata=metadata)
        self.runs.add_step(
            run_id,
            "run_finished",
            f"Run completed — {done} of {len(workspace.tasks)} steps.",
            detail={"artifacts": produced, "sources": len(workspace.sources)},
        )
        self._publish(self.get(run_id), message="Run completed")

    def _settle(self, run_id: str, status: str, message: str) -> WorkspaceRun:
        run = self.runs.update(run_id, status=status, error=message)
        assert run is not None
        self.runs.add_step(run_id, "run_finished" if not run.active else "note", message)
        self._publish(run, message=message)
        return run

    def _note(self, run: WorkspaceRun, message: str) -> None:
        self.runs.add_step(run.id, "note", message)
        self._publish(run, message=message)

    def _publish(self, run: WorkspaceRun, *, task_id: str = "", message: str = "") -> None:
        self.context.events.publish(
            WorkspaceRunUpdated(
                workspace_id=run.workspace_id,
                run_id=run.id,
                status=run.status,
                task_id=task_id or run.current_task_id,
                message=message,
            )
        )


# ------------------------------------------------------------------ prompts

_TASK_TEMPLATE = """You are executing step {position} of {total} in a workspace plan.

Overall goal: {goal}
This step: {task}"""

_TASK_CONTRACT = """Work only this step — the executor runs the next one, and doing it early \
leaves the plan describing work that is already done. Start with workspace_read so you build on \
what is there rather than restating it.

Produce something real: this step should end with an artifact created or updated in the workspace \
folder, or with a finding recorded in the step's own summary if the step was genuinely only \
investigation. Cite what you read. If the step cannot be done — a file is missing, a fact cannot \
be established, a tool is unavailable — say so plainly and stop rather than inventing a result; \
the user is asked what to do next, which beats a fabricated answer every time.

Finish with a short summary: what you did, where it landed, and anything the next step needs to \
know."""

_STEER_TEMPLATE = """The user sent new direction while the plan was running:

{direction}

The plan right now:
{plan}

Update the plan to reflect this using workspace_plan (restate every step, including the finished \
ones — their status is preserved by matching the text) or workspace_task for a single step. Keep \
completed work: do not remove or reword steps that are already done, and do not schedule them \
again. Add, reword, or reorder only what is still ahead.

Then finish with one sentence saying what you changed. Do not carry out the work itself — the \
executor does that, step by step, straight after this."""


# ------------------------------------------------------------------ helpers


def _next_task(tasks: list[WorkspaceTask]) -> WorkspaceTask | None:
    """The first pending step whose prerequisites are all done.

    Ordinary plan order covers almost everything, so a step with no declared
    dependencies is eligible as soon as it is reached. ``depends_on`` exists for
    the plans where order alone is not the truth.
    """
    done = {task.id for task in tasks if task.status == "completed"}
    for task in sorted(tasks, key=lambda item: item.position):
        if task.status != "pending":
            continue
        if all(dependency in done for dependency in task.depends_on):
            return task
    return None


def _blocked(tasks: list[WorkspaceTask]) -> bool:
    """Whether pending steps remain that nothing will ever make eligible."""
    return any(task.status == "pending" for task in tasks)


def _position(tasks: list[WorkspaceTask], task: WorkspaceTask) -> int:
    ordered = sorted(tasks, key=lambda item: item.position)
    return next((index + 1 for index, item in enumerate(ordered) if item.id == task.id), 1)


def _plan_text(tasks: list[WorkspaceTask]) -> str:
    marks = {"completed": "[done]", "in_progress": "[running]", "failed": "[failed]"}
    return "\n".join(
        f"{marks.get(task.status, '[ ]')} {task.content}"
        for task in sorted(tasks, key=lambda item: item.position)
    )


def _workspace_artifact(paths: list[str], folder: str) -> str:
    """The first touched path that belongs to this workspace, if any."""
    prefix = f"{folder.strip('/')}/"
    return next((path for path in paths if path.startswith(prefix)), "")


def _one_line(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
