"""Team orchestration: roster validation, scope isolation, and wave execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vasuki.agents.team import (
    MAX_TEAM_MEMBERS,
    TeamPlanError,
    TeamRunner,
    validate_team_plan,
)
from vasuki.model_router import ModelRole
from vasuki.schemas import (
    AgentAction,
    ContextBundle,
    TeamMember,
    TeamMemberRole,
    TeamPlan,
)
from vasuki.tools import ActionExecutor, EditTools
from vasuki.tools.editing import literal_prefix, patterns_overlap, scope_matches


def member(
    identifier: str,
    *,
    role: TeamMemberRole = "builder",
    scope: list[str] | None = None,
    read_only: bool = False,
    dependencies: list[str] | None = None,
) -> TeamMember:
    return TeamMember(
        id=identifier,
        role=role,
        objective=f"do {identifier}",
        scope=scope if scope is not None else ([] if read_only else [f"{identifier}/**"]),
        read_only=read_only,
        dependencies=dependencies or [],
    )


def context() -> ContextBundle:
    return ContextBundle(task="team work", acceptance_criteria=["done"])


# --------------------------------------------------------------------------
# Scope matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("api/**", "api/routes.py", True),
        ("api/**", "api/v1/routes.py", True),
        ("api/**", "web/routes.py", False),
        ("api/routes.py", "api/routes.py", True),
        ("api/routes.py", "api/other.py", False),
        ("**/*.py", "deep/nested/mod.py", True),
        # A single star stays inside one segment, so it must not leak into
        # subdirectories the member was never granted.
        ("src/*.py", "src/nested/mod.py", False),
        ("src/*.py", "src/mod.py", True),
    ],
)
def test_scope_matches(pattern: str, path: str, expected: bool) -> None:
    assert scope_matches(pattern, path) is expected


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("api/**", "web/**", False),
        ("api/**", "api/routes/**", True),
        ("api/**", "api/routes.py", True),
        ("api/a.py", "api/b.py", False),
        ("api/a.py", "api/a.py", True),
        ("src/*.py", "src/*.ts", False),
        # A pattern with no literal prefix could match anything, so it has to be
        # treated as overlapping rather than quietly cleared.
        ("**/*.py", "api/**", True),
    ],
)
def test_patterns_overlap(first: str, second: str, expected: bool) -> None:
    assert patterns_overlap(first, second) is expected
    assert patterns_overlap(second, first) is expected


def test_literal_prefix_stops_at_the_first_glob() -> None:
    assert literal_prefix("api/v1/**") == "api/v1"
    assert literal_prefix("api/routes.py") == "api/routes.py"
    assert literal_prefix("**/conftest.py") == ""


# --------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------


def test_waves_group_independent_members_together() -> None:
    plan = TeamPlan(
        summary="s",
        members=[
            member("scout", role="architect", read_only=True),
            member("api", dependencies=["scout"]),
            member("web", dependencies=["scout"]),
            member("docs", role="reviewer", dependencies=["api", "web"]),
        ],
    )
    waves = validate_team_plan(plan)
    assert [[item.id for item in wave] for wave in waves] == [
        ["scout"],
        ["api", "web"],
        ["docs"],
    ]


def test_overlapping_scopes_in_one_wave_are_rejected() -> None:
    plan = TeamPlan(
        summary="s",
        members=[member("a", scope=["api/**"]), member("b", scope=["api/routes/**"])],
    )
    with pytest.raises(TeamPlanError, match="scopes overlap"):
        validate_team_plan(plan)


def test_overlapping_scopes_are_allowed_when_sequenced() -> None:
    """The check is per wave: members that cannot run together cannot collide."""
    plan = TeamPlan(
        summary="s",
        members=[
            member("a", scope=["api/**"]),
            member("b", scope=["api/routes/**"], dependencies=["a"]),
        ],
    )
    assert [[item.id for item in wave] for wave in validate_team_plan(plan)] == [["a"], ["b"]]


def test_dependency_cycle_is_rejected() -> None:
    plan = TeamPlan(
        summary="s",
        members=[
            member("a", dependencies=["b"]),
            member("b", dependencies=["a"]),
        ],
    )
    with pytest.raises(TeamPlanError, match="dependency cycle"):
        validate_team_plan(plan)


def test_writer_without_scope_is_rejected() -> None:
    plan = TeamPlan(summary="s", members=[member("a", scope=[])])
    with pytest.raises(TeamPlanError, match="declares no scope"):
        validate_team_plan(plan)


def test_read_only_member_with_scope_is_rejected() -> None:
    plan = TeamPlan(summary="s", members=[member("a", read_only=True, scope=["api/**"])])
    with pytest.raises(TeamPlanError, match="read-only but declares a write scope"):
        validate_team_plan(plan)


def test_unknown_dependency_is_rejected() -> None:
    plan = TeamPlan(summary="s", members=[member("a", dependencies=["ghost"])])
    with pytest.raises(TeamPlanError, match="unknown member"):
        validate_team_plan(plan)


def test_duplicate_ids_are_rejected() -> None:
    plan = TeamPlan(summary="s", members=[member("a"), member("a")])
    with pytest.raises(TeamPlanError, match="Duplicate member id"):
        validate_team_plan(plan)


def test_empty_and_oversized_rosters_are_rejected() -> None:
    with pytest.raises(TeamPlanError, match="no members"):
        validate_team_plan(TeamPlan(summary="s", members=[]))
    oversized = TeamPlan(
        summary="s", members=[member(f"m{index}") for index in range(MAX_TEAM_MEMBERS + 1)]
    )
    with pytest.raises(TeamPlanError, match="maximum is"):
        validate_team_plan(oversized)


def test_team_member_roles_track_model_roles() -> None:
    """Every routed role is available to a team except deployment."""
    declared = set(TeamMemberRole.__args__)  # type: ignore[attr-defined]
    assert declared == {role.value for role in ModelRole} - {ModelRole.DEPLOYER.value}


# --------------------------------------------------------------------------
# Scope and read-only enforcement at the tool layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_editor_refuses_writes_outside_its_lane(tmp_path: Path) -> None:
    editor = EditTools(tmp_path, ["api/**"])
    executor = ActionExecutor(editor)

    allowed, paths = await executor.execute(
        AgentAction(thought="t", action="write", path="api/routes.py", content="ok")
    )
    assert allowed.success and paths == ["api/routes.py"]

    denied, denied_paths = await executor.execute(
        AgentAction(thought="t", action="write", path="web/app.py", content="nope")
    )
    assert not denied.success
    assert denied_paths == []
    assert not (tmp_path / "web/app.py").exists()


@pytest.mark.asyncio
async def test_deleting_outside_scope_is_refused(tmp_path: Path) -> None:
    """Deletion is a mutation and must clear the same gate as a write."""
    (tmp_path / "web").mkdir()
    victim = tmp_path / "web" / "app.py"
    victim.write_text("keep me", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, ["api/**"]))

    result, paths = await executor.execute(
        AgentAction(thought="t", action="delete", path="web/app.py")
    )

    assert not result.success
    assert paths == []
    assert victim.read_text(encoding="utf-8") == "keep me"


@pytest.mark.asyncio
async def test_read_only_member_cannot_write_even_with_empty_scope(tmp_path: Path) -> None:
    """An empty scope means 'anything' to EditTools, so read_only carries the ban."""
    executor = ActionExecutor(EditTools(tmp_path, read_only=True))

    result, paths = await executor.execute(
        AgentAction(thought="t", action="write", path="anywhere.py", content="nope")
    )

    assert not result.success
    assert "read-only" in (result.error or "")
    assert paths == []
    assert not (tmp_path / "anywhere.py").exists()


@pytest.mark.asyncio
async def test_read_only_member_can_still_read(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("findings", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, read_only=True))

    result, _ = await executor.execute(
        AgentAction(thought="t", action="read_file", path="notes.md")
    )

    assert result.success
    assert result.data["content"] == "findings"


# --------------------------------------------------------------------------
# Running a team
# --------------------------------------------------------------------------


class ScriptedGateway:
    """Gateway double that returns one write-then-finish script per role."""

    def __init__(self) -> None:
        self.structured_calls: list[str] = []
        self.concurrent = 0
        self.peak_concurrent = 0
        self._turn: dict[str, int] = {}

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: object,
        schema: type[Any],
        **kwargs: object,
    ) -> Any:
        # The task text carries the member's objective, which is how the double
        # tells members apart without the loop having to pass the id.
        task = str(messages[1].content)  # type: ignore[index]
        name = task.split('"task": "do ', 1)[-1].split("\\n", 1)[0]
        self.structured_calls.append(name)
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            # Yield control so genuinely concurrent members overlap here.
            await asyncio.sleep(0)
            turn = self._turn.get(name, 0)
            self._turn[name] = turn + 1
            if turn == 0:
                return AgentAction(
                    thought="write",
                    action="write",
                    path=f"{name}/out.txt",
                    content=name,
                )
            return AgentAction(thought="done", action="finish", summary=f"{name} done")
        finally:
            self.concurrent -= 1


@pytest.mark.asyncio
async def test_independent_members_run_concurrently_and_write_their_own_files(
    tmp_path: Path,
) -> None:
    plan = TeamPlan(summary="s", members=[member("api"), member("web")])
    gateway = ScriptedGateway()

    outcome = await TeamRunner(
        gateway,  # type: ignore[arg-type]
        tmp_path,
        require_read_before_write=False,
    ).run("mission-1", plan, context())

    assert outcome.changed == ["api/out.txt", "web/out.txt"]
    assert (tmp_path / "api/out.txt").read_text(encoding="utf-8") == "api"
    assert (tmp_path / "web/out.txt").read_text(encoding="utf-8") == "web"
    assert all(item.success for item in outcome.members)
    # Both members were inside the gateway at the same moment.
    assert gateway.peak_concurrent == 2


@pytest.mark.asyncio
async def test_dependent_member_receives_its_predecessor_summary(tmp_path: Path) -> None:
    seen: list[str] = []

    class RecordingGateway(ScriptedGateway):
        async def structured(
            self,
            mission_id: str,
            role: object,
            messages: object,
            schema: type[Any],
            **kwargs: object,
        ) -> Any:
            seen.append(str(messages[1].content))  # type: ignore[index]
            return await super().structured(mission_id, role, messages, schema, **kwargs)

    plan = TeamPlan(
        summary="s",
        members=[member("api"), member("web", dependencies=["api"])],
    )

    await TeamRunner(
        RecordingGateway(),  # type: ignore[arg-type]
        tmp_path,
        require_read_before_write=False,
    ).run("mission-1", plan, context())

    web_turns = [text for text in seen if '"task": "do web' in text]
    assert web_turns, "the dependent member never ran"
    assert "builder api: api done" in web_turns[0]


@pytest.mark.asyncio
async def test_a_failing_member_skips_its_dependents_but_not_its_peers(tmp_path: Path) -> None:
    class PartlyFailingGateway(ScriptedGateway):
        async def structured(
            self,
            mission_id: str,
            role: object,
            messages: object,
            schema: type[Any],
            **kwargs: object,
        ) -> Any:
            if '"task": "do api' in str(messages[1].content):  # type: ignore[index]
                raise RuntimeError("api model unreachable")
            return await super().structured(mission_id, role, messages, schema, **kwargs)

    plan = TeamPlan(
        summary="s",
        members=[
            member("api"),
            member("web"),
            member("docs", scope=["docs/**"], dependencies=["api"]),
        ],
    )

    outcome = await TeamRunner(
        PartlyFailingGateway(),  # type: ignore[arg-type]
        tmp_path,
        require_read_before_write=False,
    ).run("mission-1", plan, context())

    results = {item.id: item for item in outcome.members}
    assert not results["api"].success
    assert "api model unreachable" in results["api"].error
    # The peer in the same wave still finished its own work.
    assert results["web"].success
    assert (tmp_path / "web/out.txt").exists()
    # The dependent never ran against work that was never produced.
    assert not results["docs"].success
    assert "Skipped" in results["docs"].error
    assert not (tmp_path / "docs").exists()


@pytest.mark.asyncio
async def test_member_actions_are_attributed_to_their_member(tmp_path: Path) -> None:
    plan = TeamPlan(summary="s", members=[member("api"), member("web")])
    observed: list[tuple[str, str]] = []

    await TeamRunner(
        ScriptedGateway(),  # type: ignore[arg-type]
        tmp_path,
        require_read_before_write=False,
    ).run(
        "mission-1",
        plan,
        context(),
        on_action=lambda item, action, result, paths: observed.append((item.id, action.action)),
    )

    assert ("api", "write") in observed
    assert ("web", "write") in observed
    assert {name for name, _ in observed} == {"api", "web"}


@pytest.mark.asyncio
async def test_runner_rejects_an_unsafe_plan_before_touching_the_workspace(
    tmp_path: Path,
) -> None:
    plan = TeamPlan(
        summary="s",
        members=[member("a", scope=["api/**"]), member("b", scope=["api/**"])],
    )
    gateway = ScriptedGateway()

    with pytest.raises(TeamPlanError):
        await TeamRunner(gateway, tmp_path).run("mission-1", plan, context())  # type: ignore[arg-type]

    assert gateway.structured_calls == []
    assert list(tmp_path.iterdir()) == []
