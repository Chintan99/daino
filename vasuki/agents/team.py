"""Plan and run a team of sub-agents for one instruction.

A single builder loop is a bottleneck whenever an instruction has independent
parts: investigating three subsystems, or changing an API and its tests, is work
that does not have to happen one step at a time. This module lets a *team lead*
propose a roster instead, then runs the members.

Two properties make running them concurrently safe rather than merely fast:

* **Disjoint scopes.** Every writing member declares the paths it may modify, and
  ``validate_team_plan`` rejects the roster before any model call if two members
  that can run at the same time could touch the same path. The scope is then
  enforced per member by ``EditTools``, so a member that ignores its objective
  still cannot write outside its lane.
* **Read-only explorers.** Investigators are constructed read-only, because an
  empty scope means "anything" to ``EditTools`` and would otherwise be the widest
  permission in the system rather than the narrowest.

Members run in dependency waves. Everything in a wave runs concurrently; the next
wave starts once the previous one settles, and a member whose dependency failed is
reported as skipped rather than run against work that was never produced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vasuki.agents.gateway import ModelGateway
from vasuki.agents.loop import OnActionCallback, ToolLoop
from vasuki.memory import MemoryManager
from vasuki.model_router import ModelRole
from vasuki.prompts import TEAM_LEAD_SYSTEM
from vasuki.schemas import (
    AgentAction,
    ContextBundle,
    Message,
    TeamMember,
    TeamMemberOutcome,
    TeamOutcome,
    TeamPlan,
    ToolResult,
)
from vasuki.tools import ActionExecutor, EditTools
from vasuki.tools.editing import patterns_overlap

#: Ceiling on roster size. A team larger than this is a planning failure rather
#: than a real division of labour, and every member costs a full conversation.
MAX_TEAM_MEMBERS = 8

#: Notified as each member finishes, so a UI can stream progress per member.
OnMemberCallback = Callable[[TeamMemberOutcome], None]

#: Notified when a member actually begins work. Skipped members never start, so
#: this fires strictly for members whose dependencies all succeeded.
OnMemberStartCallback = Callable[[TeamMember], None]

#: Notified for every action any member executes; the member is passed so the
#: caller can attribute the action in events and in the audit ledger.
MemberActionCallback = Callable[[TeamMember, AgentAction, ToolResult, list[str]], None]


class TeamPlanError(ValueError):
    """The proposed roster cannot be run safely."""


class TeamLead:
    """Turns one instruction into a roster of scoped sub-agents."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def plan(
        self,
        mission_id: str,
        instruction: str,
        context: ContextBundle,
        *,
        profile_override: str = "",
    ) -> TeamPlan:
        known = "\n".join(context.included_paths) or "(no files in context)"
        return await self.gateway.structured(
            mission_id,
            ModelRole.PLANNER,
            [
                Message(role="system", content=TEAM_LEAD_SYSTEM),
                Message(
                    role="user",
                    content=f"Instruction:\n{instruction}\n\nFiles already in context:\n{known}\n",
                ),
            ],
            TeamPlan,
            included_files=context.included_paths,
            profile_override=profile_override or None,
        )


def validate_team_plan(plan: TeamPlan) -> list[list[TeamMember]]:
    """Check a roster and group it into dependency waves.

    Raises ``TeamPlanError`` rather than returning partial results: a roster with
    a cycle or an overlapping scope has no safe subset to run.
    """
    members = plan.members
    if not members:
        raise TeamPlanError("The team plan has no members.")
    if len(members) > MAX_TEAM_MEMBERS:
        raise TeamPlanError(
            f"The team plan has {len(members)} members; the maximum is {MAX_TEAM_MEMBERS}."
        )

    by_id: dict[str, TeamMember] = {}
    for member in members:
        if member.id in by_id:
            raise TeamPlanError(f"Duplicate member id {member.id!r}.")
        by_id[member.id] = member

    for member in members:
        if member.read_only and member.scope:
            raise TeamPlanError(f"Member {member.id!r} is read-only but declares a write scope.")
        if not member.read_only and not member.scope:
            raise TeamPlanError(
                f"Member {member.id!r} writes but declares no scope, so it could touch any "
                "file and cannot be checked against its peers."
            )
        for dependency in member.dependencies:
            if dependency == member.id:
                raise TeamPlanError(f"Member {member.id!r} depends on itself.")
            if dependency not in by_id:
                raise TeamPlanError(
                    f"Member {member.id!r} depends on unknown member {dependency!r}."
                )

    waves = _dependency_waves(members, by_id)
    for wave in waves:
        _reject_overlapping_scopes(wave)
    return waves


def _dependency_waves(
    members: list[TeamMember], by_id: dict[str, TeamMember]
) -> list[list[TeamMember]]:
    """Group members into successive waves of concurrently runnable work."""
    remaining = {member.id for member in members}
    done: set[str] = set()
    waves: list[list[TeamMember]] = []
    while remaining:
        ready = [
            by_id[identifier]
            for identifier in sorted(remaining)
            if all(dependency in done for dependency in by_id[identifier].dependencies)
        ]
        if not ready:
            stuck = ", ".join(sorted(remaining))
            raise TeamPlanError(f"The team plan has a dependency cycle among: {stuck}.")
        waves.append(ready)
        for member in ready:
            remaining.discard(member.id)
            done.add(member.id)
    return waves


def _reject_overlapping_scopes(wave: list[TeamMember]) -> None:
    """Members running at the same time must not share a writable path."""
    writers = [member for member in wave if not member.read_only]
    for index, first in enumerate(writers):
        for second in writers[index + 1 :]:
            for left in first.scope:
                for right in second.scope:
                    if patterns_overlap(left, right):
                        raise TeamPlanError(
                            f"Members {first.id!r} and {second.id!r} run at the same time but "
                            f"their scopes overlap ({left!r} and {right!r}). Give them disjoint "
                            "paths, or make one depend on the other."
                        )


def _skipped(member: TeamMember, blocked: list[str]) -> TeamMemberOutcome:
    return TeamMemberOutcome(
        id=member.id,
        role=member.role,
        objective=member.objective,
        summary="",
        success=False,
        error=f"Skipped: depends on {', '.join(sorted(blocked))}, which did not succeed.",
    )


class TeamRunner:
    """Runs a validated roster against one workspace."""

    def __init__(
        self,
        gateway: ModelGateway,
        root: Path,
        *,
        max_steps: int | None = None,
        require_read_before_write: bool = True,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        action_schema: type[AgentAction] = AgentAction,
        memory: MemoryManager | None = None,
        memory_task_id: str | None = None,
        memory_session_id: str | None = None,
    ) -> None:
        self.gateway = gateway
        self.root = root
        self.max_steps = max_steps
        self.require_read_before_write = require_read_before_write
        self.system = system
        self.tools = tools
        self.action_schema = action_schema
        self.memory = memory
        self.memory_task_id = memory_task_id
        self.memory_session_id = memory_session_id

    async def run(
        self,
        mission_id: str,
        plan: TeamPlan,
        base_context: ContextBundle,
        *,
        on_action: MemberActionCallback | None = None,
        on_member: OnMemberCallback | None = None,
        on_member_start: OnMemberStartCallback | None = None,
    ) -> TeamOutcome:
        waves = validate_team_plan(plan)
        completed: dict[str, TeamMemberOutcome] = {}
        changed: list[str] = []
        for wave in waves:
            settled = await asyncio.gather(
                *(
                    self._run_member(
                        mission_id, member, base_context, completed, on_action, on_member_start
                    )
                    for member in wave
                )
            )
            for outcome in settled:
                completed[outcome.id] = outcome
                changed.extend(outcome.changed)
                if on_member is not None:
                    on_member(outcome)
        ordered = [completed[member.id] for wave in waves for member in wave]
        return TeamOutcome(plan=plan, members=ordered, changed=sorted(set(changed)))

    async def _run_member(
        self,
        mission_id: str,
        member: TeamMember,
        base_context: ContextBundle,
        completed: dict[str, TeamMemberOutcome],
        on_action: MemberActionCallback | None,
        on_member_start: OnMemberStartCallback | None = None,
    ) -> TeamMemberOutcome:
        blocked = [
            dependency
            for dependency in member.dependencies
            if dependency not in completed or not completed[dependency].success
        ]
        if blocked:
            return _skipped(member, blocked)
        if on_member_start is not None:
            on_member_start(member)

        editor = EditTools(
            self.root,
            list(member.scope) or None,
            require_read_before_write=self.require_read_before_write,
            seen_files=set(base_context.included_paths),
            read_only=member.read_only,
        )
        loop = ToolLoop(
            self.gateway,
            ModelRole(member.role),
            ActionExecutor(
                editor,
                memory=self.memory,
                memory_task_id=self.memory_task_id,
                memory_session_id=self.memory_session_id,
            ),
            max_steps=self.max_steps,
            system=self.system,
            tools=self.tools,
            action_schema=self.action_schema,
        )
        try:
            outcome = await loop.run(
                mission_id,
                self._member_context(member, base_context, completed),
                on_action=_attribute(member, on_action),
            )
        except Exception as exc:  # noqa: BLE001 - one member must not abort its peers
            return TeamMemberOutcome(
                id=member.id,
                role=member.role,
                objective=member.objective,
                summary="",
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return TeamMemberOutcome(
            id=member.id,
            role=member.role,
            objective=member.objective,
            summary=outcome.implementation.summary,
            changed=outcome.changed,
            steps=outcome.steps,
            success=outcome.completed,
            error=(
                "The agent exhausted its step budget before finishing."
                if not outcome.completed
                else ""
            ),
        )

    def _member_context(
        self,
        member: TeamMember,
        base_context: ContextBundle,
        completed: dict[str, TeamMemberOutcome],
    ) -> ContextBundle:
        """Narrow the shared context to one member's objective and inherited findings."""
        briefings = [
            f"{completed[dependency].role} {dependency}: {completed[dependency].summary[:4_000]}"
            for dependency in member.dependencies
            if dependency in completed and completed[dependency].summary
        ]
        scope = ", ".join(member.scope) if member.scope else "read-only, no file changes"
        inherited = (
            "\n\nCompleted dependency findings:\n" + "\n\n".join(briefings) if briefings else ""
        )
        return base_context.model_copy(
            update={
                "task": f"{member.objective}{inherited}\n\nYour editable scope: {scope}",
            }
        )


def _attribute(
    member: TeamMember, on_action: MemberActionCallback | None
) -> OnActionCallback | None:
    """Tag a member's loop actions with the member before handing them upward."""
    if on_action is None:
        return None

    def forward(action: AgentAction, result: ToolResult, paths: list[str]) -> None:
        on_action(member, action, result, paths)

    return forward
