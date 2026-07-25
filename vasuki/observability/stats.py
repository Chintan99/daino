"""Telemetry aggregation from durable database records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vasuki.persistence.models import (
    DeploymentRun,
    Mission,
    ModelCall,
    Task,
    ToolCall,
    VerificationRun,
)


def collect_stats(session: Session, mission_id: str | None = None) -> dict[str, Any]:
    mission_count = session.scalar(select(func.count(Mission.id))) or 0
    calls_query = select(func.count(ModelCall.id))
    tokens_query = select(
        func.coalesce(func.sum(ModelCall.input_tokens), 0),
        func.coalesce(func.sum(ModelCall.output_tokens), 0),
        func.coalesce(func.sum(ModelCall.estimated_cost), 0.0),
    )
    tools_query = select(func.count(ToolCall.id))
    failures_query = select(func.count(VerificationRun.id)).where(VerificationRun.passed.is_(False))
    if mission_id is not None:
        calls_query = calls_query.where(ModelCall.mission_id == mission_id)
        tokens_query = tokens_query.where(ModelCall.mission_id == mission_id)
        tools_query = tools_query.where(ToolCall.mission_id == mission_id)
        failures_query = failures_query.where(VerificationRun.mission_id == mission_id)
    calls = session.scalar(calls_query) or 0
    tokens = session.execute(tokens_query).one()
    tool_calls = session.scalar(tools_query) or 0
    failures = session.scalar(failures_query) or 0
    mission_rows = session.scalars(select(Mission).order_by(Mission.created_at)).all()
    task_query = select(Task)
    deployment_query = select(DeploymentRun)
    if mission_id is not None:
        task_query = task_query.where(Task.mission_id == mission_id)
        deployment_query = deployment_query.where(DeploymentRun.mission_id == mission_id)
    tasks = session.scalars(task_query).all()
    deployments = session.scalars(deployment_query).all()

    def durations(rows: list[Any]) -> list[float]:
        return [max(0.0, (row.updated_at - row.created_at).total_seconds()) for row in rows]

    mission_durations = durations(
        [item for item in mission_rows if mission_id is None or item.id == mission_id]
    )
    task_durations = durations(list(tasks))
    deployment_durations = durations(list(deployments))
    return {
        "missions": mission_count,
        "model_calls": calls,
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "estimated_cost": float(tokens[2]),
        "tool_calls": tool_calls,
        "verification_failures": failures,
        "repair_attempts": sum(task.attempt_count for task in tasks),
        "mission_duration_seconds": sum(mission_durations),
        "task_duration_seconds": sum(task_durations),
        "deployment_duration_seconds": sum(deployment_durations),
        "average_provider_latency_ms": float(
            session.scalar(
                select(func.coalesce(func.avg(ModelCall.latency_ms), 0.0)).where(
                    ModelCall.mission_id == mission_id
                    if mission_id is not None
                    else ModelCall.id.is_not(None)
                )
            )
            or 0
        ),
    }
