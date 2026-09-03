"""Mid-turn delegation: the model splits work, safely and without recursion."""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.agents.delegation import DelegationRunner, render_delegation
from daino.agents.tool_schemas import (
    AGENT_TOOL_SPECS,
    DELEGATE,
    MAX_DELEGATES,
    MUTATING_ACTIONS,
    PLANNING_TOOL_SPECS,
    tool_call_to_action,
)
from daino.schemas import AgentAction, ContextBundle, DelegateSpec, ToolCall, ToolResult
from daino.tools import ActionExecutor, EditTools


class ScriptedGateway:
    """Answers every subagent with the same short script."""

    def __init__(self, summary: str = "looked at it") -> None:
        self.summary = summary
        self.objectives: list[str] = []

    def route_supports_tools(self, role: object, context: object = None) -> bool:
        return True

    async def complete(self, *args: object, **kwargs: object) -> object:
        from daino.schemas import LLMResponse, Message

        for value in (*args, *kwargs.values()):
            if isinstance(value, list) and all(isinstance(item, Message) for item in value):
                self.objectives.append(value[-1].content)
                break
        return LLMResponse(
            content="",
            model="mock",
            provider="mock",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="finish",
                    arguments={"thought": "done", "summary": self.summary},
                )
            ],
        )


def context() -> ContextBundle:
    return ContextBundle(task="Investigate", acceptance_criteria=[])


@pytest.mark.asyncio
async def test_two_investigators_run_and_report_back(tmp_path: Path) -> None:
    gateway = ScriptedGateway("the auth module uses JWTs")
    runner = DelegationRunner(
        gateway,  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
    )
    result = await runner(
        [
            DelegateSpec(objective="Investigate the auth module"),
            DelegateSpec(objective="Investigate the billing module"),
        ]
    )
    assert result.success
    reports = result.data["reports"]
    assert len(reports) == 2
    assert all(report["success"] for report in reports)
    assert all(report["role"] == "researcher" for report in reports)
    # Each subagent got its own objective and nothing of the other's.
    assert any("auth module" in item for item in gateway.objectives)
    assert any("billing module" in item for item in gateway.objectives)


@pytest.mark.asyncio
async def test_overlapping_writer_scopes_are_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway()
    runner = DelegationRunner(
        gateway,  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
    )
    result = await runner(
        [
            DelegateSpec(objective="Change the API", scope=["src/**"], read_only=False),
            DelegateSpec(objective="Change the models", scope=["src/models.py"], read_only=False),
        ]
    )
    assert not result.success
    assert "non-overlapping scope" in (result.error or "")
    # Nothing was spent: no subagent was ever asked for a completion.
    assert gateway.objectives == []


@pytest.mark.asyncio
async def test_a_writer_with_no_scope_is_refused(tmp_path: Path) -> None:
    """An empty scope means 'anything' to EditTools — the widest permission."""
    runner = DelegationRunner(
        ScriptedGateway(),  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
    )
    result = await runner([DelegateSpec(objective="Fix it", read_only=False)])
    assert not result.success


@pytest.mark.asyncio
async def test_the_per_turn_limit_stops_a_delegation_loop(tmp_path: Path) -> None:
    runner = DelegationRunner(
        ScriptedGateway(),  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
        max_delegations=2,
    )
    for _ in range(2):
        assert (await runner([DelegateSpec(objective="Look")])).success
    blocked = await runner([DelegateSpec(objective="Look again")])
    assert not blocked.success
    assert "already delegated" in (blocked.error or "")


@pytest.mark.asyncio
async def test_a_rejected_roster_does_not_consume_the_turn_budget(tmp_path: Path) -> None:
    """A refused delegation is the agent's mistake to fix, not a spent attempt."""
    runner = DelegationRunner(
        ScriptedGateway(),  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
        max_delegations=1,
    )
    rejected = await runner([DelegateSpec(objective="Fix it", read_only=False)])
    assert not rejected.success
    assert runner.used == 0
    assert (await runner([DelegateSpec(objective="Look")])).success


@pytest.mark.asyncio
async def test_too_many_delegates_at_once_is_refused(tmp_path: Path) -> None:
    runner = DelegationRunner(
        ScriptedGateway(),  # type: ignore[arg-type]
        tmp_path,
        mission_id="mission-1",
        context=context(),
    )
    result = await runner(
        [DelegateSpec(objective=f"Task {index}") for index in range(MAX_DELEGATES + 1)]
    )
    assert not result.success
    assert str(MAX_DELEGATES) in (result.error or "")


@pytest.mark.asyncio
async def test_an_executor_without_the_callback_refuses_to_delegate(tmp_path: Path) -> None:
    """How recursion is prevented: a subagent's executor simply has no runner."""
    executor = ActionExecutor(EditTools(tmp_path))
    result, paths = await executor.execute(
        AgentAction(
            thought="split it",
            action="delegate",
            delegates=[DelegateSpec(objective="Look at auth")],
        )
    )
    assert not result.success
    assert "already running as a subagent" in (result.error or "")
    assert paths == []


@pytest.mark.asyncio
async def test_delegated_edits_are_reported_as_this_turn_s_changes(tmp_path: Path) -> None:
    async def delegate(specs: list[DelegateSpec]) -> ToolResult:
        assert [spec.objective for spec in specs] == ["Rename the helper"]
        return ToolResult(
            tool="delegate",
            success=True,
            data={"changed": ["src/helper.py"], "reports": []},
        )

    executor = ActionExecutor(EditTools(tmp_path), delegate=delegate)
    result, paths = await executor.execute(
        AgentAction(
            thought="split it",
            action="delegate",
            delegates=[DelegateSpec(objective="Rename the helper")],
        )
    )
    assert result.success
    # The diff and the change ledger must show a delegated edit like any other.
    assert paths == ["src/helper.py"]


def test_the_delegate_tool_is_not_offered_to_a_subagent() -> None:
    names = {spec["function"]["name"] for spec in AGENT_TOOL_SPECS}
    assert "delegate" not in names
    assert DELEGATE["function"]["name"] == "delegate"


def test_delegation_is_absent_from_a_read_only_surface() -> None:
    """A planning turn must not be able to change things through a proxy."""
    assert "delegate" in MUTATING_ACTIONS
    names = {spec["function"]["name"] for spec in PLANNING_TOOL_SPECS}
    assert "delegate" not in names


def test_a_native_delegate_call_validates_into_the_action() -> None:
    action = tool_call_to_action(
        ToolCall(
            id="c1",
            name="delegate",
            arguments={
                "thought": "three subsystems",
                "delegates": [
                    {"objective": "Look at auth"},
                    {"objective": "Change billing", "scope": ["billing/**"], "read_only": False},
                ],
            },
        )
    )
    assert action.action == "delegate"
    assert action.delegates[0].read_only is True
    assert action.delegates[1].scope == ["billing/**"]


def test_reports_render_as_prose_the_agent_can_act_on() -> None:
    rendered = render_delegation(
        ToolResult(
            tool="delegate",
            success=True,
            data={
                "changed": ["a.py"],
                "reports": [
                    {
                        "id": "delegate-1",
                        "role": "researcher",
                        "objective": "Look at auth",
                        "success": True,
                        "summary": "It uses JWTs.",
                        "changed": [],
                    },
                    {
                        "id": "delegate-2",
                        "role": "builder",
                        "objective": "Fix billing",
                        "success": False,
                        "error": "ran out of steps",
                    },
                ],
            },
        )
    )
    assert "SUBAGENT REPORTS" in rendered
    assert "It uses JWTs." in rendered
    assert "FAILED: ran out of steps" in rendered
    assert "Files changed by this delegation: a.py" in rendered
