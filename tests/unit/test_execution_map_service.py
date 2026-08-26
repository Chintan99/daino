from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daino.application import ExecutionMapApplicationService
from daino.application.context import ProjectContext
from daino.events import EventBus
from daino.persistence import Database
from daino.persistence.models import Mission, MissionEventRecord, ModelCall, Project, ToolCall


def _service(
    project: tuple[Path, object, Database],
) -> tuple[ExecutionMapApplicationService, Database, str]:
    root, raw_settings, database = project
    context = ProjectContext(root, raw_settings, database, EventBus())  # type: ignore[arg-type]
    return ExecutionMapApplicationService(context), database, database.project().id


def _mission(
    project_id: str,
    mission_id: str,
    request: str,
    created_at: datetime,
    *,
    status: str = "completed",
) -> Mission:
    return Mission(
        id=mission_id,
        project_id=project_id,
        request=request,
        mode="direct",
        status=status,
        workspace_path=None,
        branch=None,
        initial_revision=None,
        final_revision=None,
        failure=None,
        created_at=created_at,
    )


def test_prompt_index_is_newest_first_and_aggregates_each_mission(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    then = datetime(2026, 8, 25, 9, tzinfo=UTC)
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-old", "First prompt", then),
                _mission(
                    project_id,
                    "mission-new",
                    "Second prompt with token=visible-secret",
                    then + timedelta(minutes=1),
                    status="running",
                ),
                ModelCall(
                    id="model-new",
                    mission_id="mission-new",
                    role="builder",
                    provider="local",
                    model="coder",
                    selection_reason="configured",
                    input_tokens=80,
                    output_tokens=20,
                    latency_ms=125,
                    estimated_cost=0.04,
                    success=True,
                ),
                ToolCall(
                    id="tool-new",
                    mission_id="mission-new",
                    tool="agent.read_file",
                    arguments={},
                    result_summary="ok",
                    duration_seconds=0.2,
                    success=True,
                ),
                MissionEventRecord(
                    id="event-new",
                    project_id=project_id,
                    mission_id="mission-new",
                    kind="MissionCreated",
                    payload={"mode": "direct"},
                ),
            ]
        )

    prompts = service.prompts()

    assert [item.mission_id for item in prompts] == ["mission-new", "mission-old"]
    assert prompts[0].request == "Second prompt with token=[REDACTED]"
    assert prompts[0].status == "running"
    assert prompts[0].total_tokens == 100
    assert prompts[0].estimated_cost == pytest.approx(0.04)
    assert prompts[0].model_call_count == 1
    assert prompts[0].tool_count == 1
    assert prompts[0].step_count == 3
    assert prompts[1].total_tokens == 0


def test_trace_merges_events_models_and_tools_chronologically_with_usage(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    then = datetime(2026, 8, 25, 10, tzinfo=UTC)
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-map", "Build the endpoint", then),
                MissionEventRecord(
                    id="event-start",
                    project_id=project_id,
                    mission_id="mission-map",
                    kind="MissionStarted",
                    payload={"workspace": "/private/workspace"},
                    created_at=then + timedelta(seconds=1),
                ),
                ModelCall(
                    id="model-plan",
                    mission_id="mission-map",
                    role="planner",
                    provider="ollama",
                    model="qwen-coder",
                    selection_reason="local route",
                    input_tokens=120,
                    output_tokens=30,
                    latency_ms=400,
                    estimated_cost=0.012,
                    success=True,
                    created_at=then + timedelta(seconds=2),
                ),
                ToolCall(
                    id="tool-read",
                    mission_id="mission-map",
                    tool="agent.read_file",
                    arguments={"path": "app.py"},
                    result_summary="ok",
                    duration_seconds=0.75,
                    success=True,
                    created_at=then + timedelta(seconds=3),
                ),
            ]
        )

    trace = service.trace("mission-map")

    assert [step.id for step in trace.steps] == [
        "event:event-start",
        "model:model-plan",
        "tool:tool-read",
    ]
    assert [step.kind for step in trace.steps] == ["mission", "model", "tool"]
    model_step = trace.steps[1]
    assert model_step.model_usage is not None
    assert model_step.model_usage.provider == "ollama"
    assert model_step.model_usage.model == "qwen-coder"
    assert model_step.model_usage.input_tokens == 120
    assert model_step.model_usage.output_tokens == 30
    assert model_step.model_usage.total_tokens == 150
    assert model_step.model_usage.estimated_cost == pytest.approx(0.012)
    assert model_step.model_usage.latency_ms == pytest.approx(400)
    assert model_step.duration_seconds == pytest.approx(0.4)
    assert trace.total_tokens == 150
    assert trace.total_model_latency_ms == pytest.approx(400)
    assert trace.total_tool_duration_seconds == pytest.approx(0.75)


def test_trace_exposes_only_allowlisted_sanitized_tool_targets(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    then = datetime(2026, 8, 25, 10, 30, tzinfo=UTC)
    secret = "sk-private-target-token-123456789"
    calls = [
        ("path", "agent.read_file", {"path": "src/app.py", "content": "hidden"}),
        (
            "query",
            "agent.grep",
            {"query": f"find this api_key={secret}", "content": "hidden"},
        ),
        ("pattern", "agent.glob", {"pattern": "src/**/*.py", "thought": "hidden"}),
        (
            "command",
            "agent.run_command",
            {"command": f"ACCESS_TOKEN={secret} pytest -q --password {secret}"},
        ),
        (
            "url",
            "agent.fetch_url",
            {"url": f"https://user:{secret}@example.com/docs?token={secret}#private"},
        ),
    ]
    with database.session() as session:
        session.add(_mission(project_id, "mission-targets", "Inspect targets", then))
        session.add_all(
            ToolCall(
                id=f"tool-{call_id}",
                mission_id="mission-targets",
                tool=tool,
                arguments=arguments,
                result_summary=f"raw result {secret}",
                duration_seconds=0.1,
                success=True,
                created_at=then + timedelta(seconds=index),
            )
            for index, (call_id, tool, arguments) in enumerate(calls, start=1)
        )

    trace = service.trace("mission-targets")

    targets = {step.id.removeprefix("tool:tool-"): step.target for step in trace.steps}
    assert targets == {
        "path": "src/app.py",
        "query": "find this api_key=[REDACTED]",
        "pattern": "src/**/*.py",
        "command": "pytest",
        "url": "https://example.com/docs",
    }
    serialized = json.dumps(asdict(trace), default=str)
    assert secret not in serialized
    assert "--password" not in serialized
    assert "raw result" not in serialized


def test_durable_calls_replace_their_matching_lifecycle_nodes(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    then = datetime(2026, 8, 25, 10, 45, tzinfo=UTC)
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-deduplicated", "Do it", then),
                MissionEventRecord(
                    id="event-model-selected",
                    project_id=project_id,
                    mission_id="mission-deduplicated",
                    kind="ModelSelected",
                    payload={"provider": "local", "model": "coder", "role": "builder"},
                    created_at=then + timedelta(seconds=1),
                ),
                MissionEventRecord(
                    id="event-tool-started",
                    project_id=project_id,
                    mission_id="mission-deduplicated",
                    kind="ToolStarted",
                    payload={"tool": "agent.read_file", "summary": "src/app.py"},
                    created_at=then + timedelta(seconds=3),
                ),
                MissionEventRecord(
                    id="event-tool-completed",
                    project_id=project_id,
                    mission_id="mission-deduplicated",
                    kind="ToolCompleted",
                    payload={"tool": "agent.read_file", "summary": "src/app.py"},
                    created_at=then + timedelta(seconds=4),
                ),
                MissionEventRecord(
                    id="event-tool-still-running",
                    project_id=project_id,
                    mission_id="mission-deduplicated",
                    kind="ToolStarted",
                    payload={"tool": "agent.grep", "summary": "needle"},
                    created_at=then + timedelta(seconds=6),
                ),
                ModelCall(
                    id="model-durable",
                    mission_id="mission-deduplicated",
                    role="builder",
                    provider="local",
                    model="coder",
                    selection_reason="configured",
                    input_tokens=10,
                    output_tokens=5,
                    latency_ms=100,
                    estimated_cost=0,
                    success=True,
                    created_at=then + timedelta(seconds=2),
                ),
                ToolCall(
                    id="tool-durable",
                    mission_id="mission-deduplicated",
                    tool="agent.read_file",
                    arguments={"path": "src/app.py"},
                    result_summary="ok",
                    duration_seconds=0.25,
                    success=True,
                    created_at=then + timedelta(seconds=5),
                ),
            ]
        )

    trace = service.trace("mission-deduplicated")

    assert [step.id for step in trace.steps] == [
        "model:model-durable",
        "tool:tool-durable",
        "event:event-tool-still-running",
    ]
    assert trace.steps[-1].status == "running"
    assert trace.steps[-1].target == "needle"
    assert service.prompts()[0].step_count == 3


def test_tool_trace_never_exposes_action_thought_content_result_or_secrets(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    secret = "sk-this-is-a-private-token-123456789"
    then = datetime(2026, 8, 25, 11, tzinfo=UTC)
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-private", "Safe prompt", then),
                ToolCall(
                    id="tool-private",
                    mission_id="mission-private",
                    tool="agent.run_command",
                    arguments={
                        "thought": "private chain of thought",
                        "content": "raw file body",
                        "old_string": "old raw source",
                        "new_string": "new raw source",
                        "command": f"deploy --token {secret}",
                    },
                    result_summary=f"authorization: Bearer {secret}\nraw command output",
                    duration_seconds=1.5,
                    success=False,
                    created_at=then + timedelta(seconds=1),
                ),
                MissionEventRecord(
                    id="event-private",
                    project_id=project_id,
                    mission_id="mission-private",
                    kind="ToolFailed",
                    payload={
                        "tool": "agent.run_command",
                        "error": f"password={secret}",
                        "thought": "event thought",
                        "content": "event raw content",
                        "details": {"secret": secret},
                    },
                    created_at=then + timedelta(seconds=2),
                ),
            ]
        )

    serialized = json.dumps(asdict(service.trace("mission-private")), default=str)

    assert "Run command" in serialized
    for private_value in (
        secret,
        "private chain of thought",
        "raw file body",
        "old raw source",
        "new raw source",
        "raw command output",
        "event thought",
        "event raw content",
    ):
        assert private_value not in serialized


def test_trace_rejects_a_mission_outside_the_current_project(
    project: tuple[Path, object, Database],
) -> None:
    service, database, _ = _service(project)
    then = datetime(2026, 8, 25, 12, tzinfo=UTC)
    with database.session() as session:
        session.add(
            Project(
                id="project-other",
                name="other",
                root_path="/other/project",
            )
        )
        session.add(
            _mission(
                "project-other",
                "mission-other-project",
                "Outside prompt",
                then,
            )
        )

    with pytest.raises(ValueError, match="Unknown mission"):
        service.trace("mission-other-project")


def test_prompt_index_returns_all_missions_unless_a_limit_is_requested(
    project: tuple[Path, object, Database],
) -> None:
    service, database, project_id = _service(project)
    then = datetime(2026, 8, 25, 13, tzinfo=UTC)
    with database.session() as session:
        session.add_all(
            _mission(
                project_id,
                f"mission-{index:03}",
                f"Prompt {index}",
                then + timedelta(seconds=index),
            )
            for index in range(105)
        )

    assert len(service.prompts()) == 105
    assert len(service.list_prompts()) == 105
    assert [item.mission_id for item in service.prompts(limit=2)] == [
        "mission-104",
        "mission-103",
    ]
