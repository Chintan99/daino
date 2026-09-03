"""Let the agent decide, mid-turn, to split work across subagents.

``TeamRunner`` already knew how to run scoped agents concurrently and safely, and
the only way to reach it was for the *user* to type ``/team``. So the capability
existed and the decision did not: an agent three steps into a task, realising it
needs four subsystems investigated, had to walk them one at a time and hope the
context held.

This is the missing half — the ``delegate`` action, backed by the same runner and
the same guarantees:

* **Scopes are checked before anything runs.** ``validate_team_plan`` refuses a
  roster whose writers could touch the same path, so two delegates cannot race
  on one file. A writer with no scope is refused rather than granted everything.
* **Read-only is the default.** The common reason to delegate is to look at
  several things at once, and an empty scope means "anything" to ``EditTools`` —
  the widest permission in the system, not the narrowest. A subagent that only
  reads needs no scope and can safely share the tree with its siblings.
* **Subagents cannot delegate.** Their executor is built without the callback and
  their tool surface without the tool, so recursion is impossible by
  construction rather than discouraged by prompt.

What bounds the cost is not a special rule here but the ordinary one: the
mission's ``RunBudget`` is keyed by mission id, and every subagent's model call
draws on it. Five delegates spend the same account five times faster, and stop
at the same wall.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from daino.agents.gateway import ModelGateway
from daino.agents.team import (
    MemberActionCallback,
    MemberActionStartCallback,
    TeamPlanError,
    TeamRunner,
)
from daino.agents.tool_schemas import AGENT_TOOL_SPECS, MAX_DELEGATES
from daino.memory import MemoryManager
from daino.observability import span
from daino.schemas import (
    ContextBundle,
    DelegateSpec,
    TeamMember,
    TeamMemberOutcome,
    TeamPlan,
    ToolResult,
)
from daino.tools.web import WebResearch

#: Delegations one turn may make. Distinct from the per-call ``MAX_DELEGATES``:
#: that stops one impulsive fan-out, this stops a loop that answers every
#: obstacle by spawning more agents.
MAX_DELEGATIONS_PER_TURN = 3

#: Steps a delegated subagent may take. Lower than a top-level run on purpose: a
#: delegate has one bounded objective, and one that needs forty steps was mis-
#: scoped by the agent that created it — better to report that back than to let
#: it wander.
DEFAULT_DELEGATE_STEPS = 24


class DelegationRunner:
    """Turns ``delegate`` actions into concurrent subagent runs."""

    def __init__(
        self,
        gateway: ModelGateway,
        root: Path,
        *,
        mission_id: str,
        context: ContextBundle,
        memory: MemoryManager | None = None,
        memory_task_id: str | None = None,
        memory_session_id: str | None = None,
        web: WebResearch | None = None,
        max_steps: int = DEFAULT_DELEGATE_STEPS,
        max_delegations: int = MAX_DELEGATIONS_PER_TURN,
        on_action: MemberActionCallback | None = None,
        on_action_start: MemberActionStartCallback | None = None,
        on_member: Callable[[TeamMemberOutcome], None] | None = None,
        on_member_start: Callable[[TeamMember], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.root = root
        self.mission_id = mission_id
        self.context = context
        self.memory = memory
        self.memory_task_id = memory_task_id
        self.memory_session_id = memory_session_id
        self.web = web
        self.max_steps = max_steps
        self.max_delegations = max_delegations
        self.on_action = on_action
        self.on_action_start = on_action_start
        self.on_member = on_member
        self.on_member_start = on_member_start
        #: Delegations made this turn, against ``max_delegations``.
        self.used = 0

    async def __call__(self, delegates: list[DelegateSpec]) -> ToolResult:
        """Run one ``delegate`` action. Never raises; failure is an observation."""
        if self.used >= self.max_delegations:
            return ToolResult(
                tool="delegate",
                success=False,
                error=(
                    f"This turn has already delegated {self.used} times, which is the limit. "
                    "Finish the remaining work yourself, or finish and let the user start a "
                    "new turn for the rest."
                ),
            )
        if len(delegates) > MAX_DELEGATES:
            return ToolResult(
                tool="delegate",
                success=False,
                error=(
                    f"At most {MAX_DELEGATES} subagents per delegation; you asked for "
                    f"{len(delegates)}. Group the objectives or run them across two steps."
                ),
            )
        members = [_as_member(index, spec) for index, spec in enumerate(delegates, start=1)]
        plan = TeamPlan(summary="Delegated by the agent mid-turn.", members=members)
        runner = TeamRunner(
            self.gateway,
            self.root,
            max_steps=self.max_steps,
            memory=self.memory,
            memory_task_id=self.memory_task_id,
            memory_session_id=self.memory_session_id,
            web=self.web,
            # The tool surface without ``delegate`` in it. Combined with the
            # executor built without the callback, a subagent has neither the
            # invitation nor the ability.
            tools=AGENT_TOOL_SPECS,
        )
        self.used += 1
        with span(
            "agent.delegate",
            **{
                "daino.mission_id": self.mission_id,
                "daino.delegates": len(members),
                "daino.read_only": all(member.read_only for member in members),
            },
        ) as recording:
            try:
                outcome = await runner.run(
                    self.mission_id,
                    plan,
                    self.context,
                    on_action=self.on_action,
                    on_action_start=self.on_action_start,
                    on_member=self.on_member,
                    on_member_start=self.on_member_start,
                )
            except TeamPlanError as exc:
                # A rejected roster is the agent's mistake to fix — overlapping
                # scopes, or a writer that named none — so it is reported in
                # terms it can act on rather than as an internal error.
                self.used -= 1
                return ToolResult(
                    tool="delegate",
                    success=False,
                    error=(
                        f"That delegation cannot run safely: {exc} "
                        "Give each writing subagent its own non-overlapping scope, or make "
                        "the overlapping ones read_only."
                    ),
                )
            recording.set_attribute("daino.changed_files", len(outcome.changed))
        succeeded = [item for item in outcome.members if item.success]
        return ToolResult(
            tool="delegate",
            # Partial success is success: the reports that did come back are
            # useful, and failing the whole action would throw them away.
            success=bool(succeeded),
            data={
                "changed": list(outcome.changed),
                "reports": [_report(item) for item in outcome.members],
            },
            error=("" if succeeded else "Every subagent failed; their errors are in the reports."),
        )


def _as_member(index: int, spec: DelegateSpec) -> TeamMember:
    """Fill in the parts of a roster entry the model should not have to invent."""
    return TeamMember(
        id=f"delegate-{index}",
        # ``read_only`` already carries the distinction, so the routing role is
        # derived from it rather than asked for again: a model that had to state
        # both could state them inconsistently, and there is no right answer to
        # a read-only builder.
        role="researcher" if spec.read_only else "builder",
        objective=spec.objective,
        scope=list(spec.scope),
        read_only=spec.read_only,
        dependencies=[],
    )


def _report(outcome: TeamMemberOutcome) -> dict[str, object]:
    return {
        "id": outcome.id,
        "role": outcome.role,
        "objective": outcome.objective,
        "success": outcome.success,
        "summary": outcome.summary,
        "changed": list(outcome.changed),
        "error": outcome.error,
    }


def render_delegation(result: ToolResult) -> str:
    """The observation text for a finished delegation.

    Written as prose per subagent rather than as a JSON dump: the agent has to
    read every report and decide what to do next, and a nested object is the
    shape it reads worst.
    """
    reports = (result.data or {}).get("reports") or []
    if not reports:
        return result.error or "No subagents ran."
    lines: list[str] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        header = f"{report.get('id')} ({report.get('role')}) — {report.get('objective', '')}"
        if report.get("success"):
            body = str(report.get("summary") or "(no summary)")
            changed = report.get("changed") or []
            if changed:
                body += "\nChanged: " + ", ".join(str(item) for item in changed)
        else:
            body = f"FAILED: {report.get('error') or 'no reason given'}"
        lines.append(f"{header}\n{body}")
    changed_all = (result.data or {}).get("changed") or []
    footer = (
        "\n\nFiles changed by this delegation: " + ", ".join(str(item) for item in changed_all)
        if changed_all
        else ""
    )
    return (
        "SUBAGENT REPORTS — these are summaries written by agents that did not see this "
        "conversation. Verify anything you are about to depend on.\n\n"
        + "\n\n".join(lines)
        + footer
    )
