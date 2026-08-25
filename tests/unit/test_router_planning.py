from __future__ import annotations

import pytest

from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings
from vasuki.context import ExecutionMode, ModelExecutionProfile
from vasuki.exceptions import ConfigurationError
from vasuki.model_router import ModelRole, ModelRouter, RoutingContext
from vasuki.planning import validate_task_graph, validate_transition
from vasuki.schemas import ProjectMode, TaskPlan, TaskSpec, TaskStatus


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
    assert standard.initial_context_tokens == 24_000
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
    assert profile.initial_context_tokens == 22_000


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
