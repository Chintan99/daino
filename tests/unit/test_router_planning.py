from __future__ import annotations

from pathlib import Path

import pytest

from daino.agents.gateway import ModelGateway
from daino.agents.tool_schemas import AGENT_TOOL_SPECS
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings
from daino.context import CapabilityEnvelope, ExecutionMode, ModelExecutionProfile
from daino.exceptions import ConfigurationError
from daino.model_router import ModelRole, ModelRouter, RoutingContext
from daino.persistence import Database
from daino.planning import Planner, validate_task_graph, validate_transition
from daino.schemas import (
    Message,
    ProjectMode,
    RequirementSpec,
    TaskPlan,
    TaskSpec,
    TaskStatus,
)


def configured_settings() -> Settings:
    settings = Settings()
    settings.providers = {
        "local": ProviderConfig(type="vllm", base_url="http://localhost:8000/v1", model="small"),
        "cloud": ProviderConfig(
            type="openai-compatible", base_url="https://example.invalid/v1", model="strong"
        ),
    }
    settings.models = {
        "small": ModelProfileConfig(provider="local", model="small", local=True),
        "strong": ModelProfileConfig(provider="cloud", model="strong", coding_score=9),
    }
    settings.routing = {"builder": "small", "debugger": "strong"}
    settings.routing_fallbacks = {"builder": ["strong"]}
    return settings


def test_router_primary_and_escalation_reason() -> None:
    router = ModelRouter(configured_settings())
    primary = router.select(ModelRole.BUILDER)
    escalated = router.select(ModelRole.BUILDER, RoutingContext(failed_attempts=2))
    assert primary.profile_name == "small"
    assert escalated.profile_name == "strong"
    assert escalated.escalated
    assert "failed twice" in escalated.reason


def test_execution_profile_auto_detects_small_local_model() -> None:
    compact = ModelExecutionProfile.resolve(
        "small",
        ModelProfileConfig(
            provider="local",
            model="7b",
            local=True,
            coding_score=3,
            tool_reliability=3,
        ),
        input_budget_tokens=20_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )
    standard = ModelExecutionProfile.resolve(
        "strong",
        ModelProfileConfig(
            provider="cloud",
            model="strong",
        ),
        input_budget_tokens=40_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )

    assert compact.mode == ExecutionMode.COMPACT
    assert compact.initial_context_tokens == 8_192
    assert compact.max_steps is None
    assert standard.mode == ExecutionMode.STANDARD
    # Roomy, but bounded by what compaction can afford to re-add every pass:
    # the bundle plus memory has to leave the transcript real room, or the
    # agent loses what it reads and re-reads it. See test_execution_profiles.
    assert standard.initial_context_tokens > compact.initial_context_tokens
    assert standard.instruction_tokens + standard.source_tokens + standard.memory_tokens < int(
        standard.input_budget_tokens * 0.8
    )
    assert standard.max_steps is None


def test_router_exposes_operational_fallback_after_primary() -> None:
    selections = ModelRouter(configured_settings()).failover_selections(ModelRole.BUILDER)
    assert [item.profile_name for item in selections] == ["small", "strong"]


def test_local_model_must_still_allow_requested_sensitivity() -> None:
    settings = configured_settings()
    settings.models["small"].data_sensitivity = "internal"
    settings.models["strong"].data_sensitivity = "internal"
    with pytest.raises(ConfigurationError, match="No model is allowed"):
        ModelRouter(settings).select(
            ModelRole.BUILDER,
            RoutingContext(data_sensitivity="restricted"),
        )


def test_task_graph_topological_order_and_cycle() -> None:
    first = TaskSpec(
        id="a",
        title="A",
        objective="A",
        acceptance_criteria=["done"],
        verification_commands=["pytest"],
    )
    second = TaskSpec(
        id="b",
        title="B",
        objective="B",
        dependencies=["a"],
        acceptance_criteria=["done"],
        verification_commands=["pytest"],
    )
    plan = TaskPlan(summary="test", mode=ProjectMode.DIRECT, tasks=[second, first])
    assert [item.id for item in validate_task_graph(plan)] == ["a", "b"]
    cyclic = plan.model_copy(
        update={
            "tasks": [
                first.model_copy(update={"dependencies": ["b"]}),
                second,
            ]
        }
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_task_graph(cyclic)


def test_task_state_machine_rejects_skip() -> None:
    validate_transition(TaskStatus.PENDING, TaskStatus.READY)
    with pytest.raises(ValueError):
        validate_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)


def test_task_repairs_argv_shaped_verification_commands() -> None:
    task = TaskSpec(
        id="a",
        title="A",
        objective="A",
        acceptance_criteria=["done"],
        verification_commands=["python", "-m", "pytest", "-q"],
    )
    separate = task.model_copy(update={"verification_commands": ["pytest", "ruff"]})

    assert task.verification_commands == ["python -m pytest -q"]
    assert separate.verification_commands == ["pytest", "ruff"]


def test_unrated_local_model_keeps_the_full_window() -> None:
    """Neutral default scores are not evidence that a local model is small.

    Treating them as such put every freshly configured local model on 8k of
    context and one action per turn, which made long tasks loop.
    """
    profile = ModelExecutionProfile.resolve(
        "local-ollama",
        ModelProfileConfig(provider="local", model="qwen3-27b", local=True),
        input_budget_tokens=22_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )

    assert profile.mode == ExecutionMode.STANDARD
    assert not profile.one_action_per_turn
    # Far above the 8k compact clamp this test exists to rule out, but not the
    # whole budget: standard mode reserves a share for the system prompt and the
    # working transcript, without which the first prompt already sits over the
    # compaction threshold and every later turn compacts for no gain.
    #
    # Asserted as the invariant rather than as a number, because the number is
    # derived from the compaction threshold and changing that must not silently
    # change what this test is protecting.
    assert profile.initial_context_tokens > 8_192
    scaffold = profile.instruction_tokens + profile.source_tokens + profile.memory_tokens
    assert scaffold < int(profile.input_budget_tokens * 0.8)


def test_a_starved_input_budget_still_forces_compact_mode() -> None:
    profile = ModelExecutionProfile.resolve(
        "local-ollama",
        ModelProfileConfig(provider="local", model="qwen3-27b", local=True),
        input_budget_tokens=9_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )

    assert profile.mode == ExecutionMode.COMPACT


# --------------------------------------- sizing tasks for the executor's window


def _requirements() -> RequirementSpec:
    return RequirementSpec(
        problem_statement="p",
        goals=["g"],
        functional_requirements=["f"],
        acceptance_criteria=["a"],
        test_strategy=["pytest"],
    )


def _split_brain_settings() -> Settings:
    """The shipped pairing: a roomy cloud planner, a narrow local builder.

    `config.example.yaml` ships exactly this — `planner: strong-cloud`,
    `builder: local-ollama` — and it is the configuration where planning in the
    planner's own terms produces tasks the builder cannot execute.
    """
    settings = Settings()
    settings.providers = {
        "local": ProviderConfig(type="vllm", base_url="http://localhost:8000/v1", model="small"),
        "cloud": ProviderConfig(
            type="openai-compatible", base_url="https://example.invalid/v1", model="strong"
        ),
    }
    settings.models = {
        "small": ModelProfileConfig(
            provider="local", model="small", local=True, context_window=32_768
        ),
        "strong": ModelProfileConfig(provider="cloud", model="strong", context_window=400_000),
    }
    settings.routing = {"planner": "strong", "builder": "small"}
    return settings


def test_the_envelope_describes_the_builder_not_the_planner(tmp_path: Path) -> None:
    """The whole point of the envelope, asserted as directly as it can be.

    A strong planner reading its own limits writes tasks a weak builder cannot
    hold — the field failure, where the builder read one 6k-token file, lost it
    to compaction, and read it again until the no-progress guard killed the run.
    """
    settings = _split_brain_settings()
    gateway = ModelGateway(settings, Database(settings, tmp_path))

    builder = gateway.capability_envelope(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
    planner = gateway.capability_envelope(ModelRole.PLANNER, tools=AGENT_TOOL_SPECS)

    assert builder.profile_name == "small"
    assert planner.profile_name == "strong"
    # Not merely different — smaller, in both the numbers a task is sized by.
    assert builder.task_source_budget_tokens < planner.task_source_budget_tokens
    assert builder.max_files_per_task <= planner.max_files_per_task


def test_the_tool_schemas_are_charged_against_the_envelope(tmp_path: Path) -> None:
    """An envelope resolved without them over-reports by what the builder pays."""
    settings = _split_brain_settings()
    gateway = ModelGateway(settings, Database(settings, tmp_path))

    with_tools = gateway.capability_envelope(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
    without = gateway.capability_envelope(ModelRole.BUILDER)

    assert with_tools.working_headroom_tokens < without.working_headroom_tokens


@pytest.mark.asyncio
async def test_the_planner_prompt_carries_the_executor_numbers(tmp_path: Path) -> None:
    settings = _split_brain_settings()
    gateway = ModelGateway(settings, Database(settings, tmp_path))
    envelope = gateway.capability_envelope(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
    recorded: list[list[Message]] = []

    class RecordingGateway:
        """Only `structured`, like the fake gateways elsewhere in the suite."""

        async def structured(self, mission_id, role, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
            recorded.append(messages)
            return TaskPlan(
                summary="s",
                mode=ProjectMode.DIRECT,
                tasks=[
                    TaskSpec(
                        id="t1",
                        title="t",
                        objective="o",
                        acceptance_criteria=["a"],
                        verification_commands=[],
                    )
                ],
            )

    await Planner(RecordingGateway()).plan(  # type: ignore[arg-type]
        "mission",
        _requirements(),
        "Repository map",
        ProjectMode.DIRECT,
        envelope=envelope,
    )

    prompt = recorded[0][1].content
    assert str(envelope.max_files_per_task) in prompt
    assert str(envelope.task_source_budget_tokens) in prompt
    # And it says whose limits these are, or the planner applies them to itself.
    assert "not you" in prompt


@pytest.mark.asyncio
async def test_planning_still_works_with_no_envelope() -> None:
    """Every duck-typed gateway in the suite calls plan() without one."""
    recorded: list[list[Message]] = []

    class RecordingGateway:
        async def structured(self, mission_id, role, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
            recorded.append(messages)
            return TaskPlan(summary="s", mode=ProjectMode.DIRECT, tasks=[])

    plan = await Planner(RecordingGateway()).plan(  # type: ignore[arg-type]
        "mission",
        _requirements(),
        "Repository map",
        ProjectMode.DIRECT,
    )

    assert plan.mode == ProjectMode.DIRECT
    assert "Executor limits" not in recorded[0][1].content


# ------------------------------- splitting through a file the model must divide


@pytest.mark.asyncio
async def test_resize_rewrites_every_id_it_is_given() -> None:
    """Ids from the model are never trusted.

    A returned id that collides with a live task, or a dependency on an id the
    model invented, fails `validate_task_graph` — which fails the whole mission,
    not just the split.
    """
    envelope = CapabilityEnvelope(
        profile_name="small",
        compact=False,
        one_action_per_turn=False,
        max_steps=None,
        working_headroom_tokens=6_000,
        source_tokens=6_000,
        max_files_per_task=1,
        task_source_budget_tokens=4_000,
    )
    parent = TaskSpec(
        id="task-huge",
        title="Rewrite the service",
        objective="Rewrite service.py",
        dependencies=["task-earlier"],
        expected_files=["service.py"],
        allowed_files=["service.py"],
        acceptance_criteria=["the service works"],
        verification_commands=["pytest"],
    )

    class ColludingGateway:
        """Returns ids that collide and dependencies that do not exist."""

        async def structured(self, mission_id, role, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
            return TaskPlan(
                summary="split",
                mode=ProjectMode.DIRECT,
                tasks=[
                    TaskSpec(
                        id="task-earlier",
                        title="Part one",
                        objective="Rewrite the parser functions",
                        dependencies=["invented-id"],
                        acceptance_criteria=["parser rewritten"],
                        verification_commands=["npm test"],
                    ),
                    TaskSpec(
                        id="task-earlier",
                        title="Part two",
                        objective="Rewrite the writer functions",
                        acceptance_criteria=["writer rewritten"],
                        verification_commands=[],
                    ),
                ],
            )

    slices = await Planner(ColludingGateway()).resize(  # type: ignore[arg-type]
        "mission", parent, envelope, "- line 1: function parse"
    )

    assert [spec.id for spec in slices] == ["task-huge-r01", "task-huge-r02"]
    assert slices[0].dependencies == ["task-earlier"]
    assert slices[1].dependencies == ["task-huge-r01"]
    assert "invented-id" not in slices[0].dependencies
    # Every part edits the same file, whatever the model said about scope.
    assert all(spec.allowed_files == ["service.py"] for spec in slices)
    # And only the last part runs the real check.
    assert slices[0].verification_commands == []
    assert slices[1].verification_commands == ["pytest"]
    assert all(spec.slice_of == "task-huge" for spec in slices)


@pytest.mark.asyncio
async def test_resize_refuses_a_single_task_answer() -> None:
    """One task is the original renamed, and re-running it would loop."""
    envelope = CapabilityEnvelope(
        profile_name="small",
        compact=False,
        one_action_per_turn=False,
        max_steps=None,
        working_headroom_tokens=6_000,
        source_tokens=6_000,
        max_files_per_task=1,
        task_source_budget_tokens=4_000,
    )
    parent = TaskSpec(
        id="task-huge",
        title="t",
        objective="o",
        expected_files=["service.py"],
        acceptance_criteria=["a"],
        verification_commands=[],
    )

    class LazyGateway:
        async def structured(self, mission_id, role, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
            return TaskPlan(
                summary="split",
                mode=ProjectMode.DIRECT,
                tasks=[
                    TaskSpec(
                        id="x",
                        title="t",
                        objective="o",
                        acceptance_criteria=["a"],
                        verification_commands=[],
                    )
                ],
            )

    assert await Planner(LazyGateway()).resize("m", parent, envelope, "") == []  # type: ignore[arg-type]


def test_a_file_outline_names_lines_and_kinds() -> None:
    """The split follows real boundaries, so it must be given them."""
    from daino.planning.planner import outline_of
    from daino.schemas import RepositorySymbol

    rendered = outline_of(
        [
            RepositorySymbol(name="write", kind="function", path="s.py", line=40),
            RepositorySymbol(name="Parser", kind="class", path="s.py", line=10),
        ]
    )

    # Sorted by line, so the outline reads in the order the file does.
    assert rendered.splitlines() == [
        "- line 10: class Parser",
        "- line 40: function write",
    ]


def test_an_unindexed_file_still_produces_an_outline() -> None:
    """The tree-sitter worker can fail; the fallback must not be a crash."""
    from daino.planning.planner import outline_of

    assert "no symbols" in outline_of([])
