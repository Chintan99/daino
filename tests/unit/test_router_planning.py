from __future__ import annotations

import pytest

from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings
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
