"""Telemetry aggregation from durable database records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from daino.persistence.models import (
    ConversationMessage,
    ConversationSession,
    DeploymentRun,
    Mission,
    ModelCall,
    Task,
    ToolCall,
    VerificationRun,
)


def collect_stats(
    session: Session,
    mission_id: str | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate telemetry for a project, mission, or conversation session.

    Model calls belong to missions, while a TUI launch creates a conversation
    session before it creates its first mission.  Treating a missing mission as
    project-wide made a fresh window inherit every token and charge previously
    recorded in that project's database.  ``session_id`` follows the missions
    referenced by that conversation instead, including its current mission and
    earlier turns preserved as conversation messages.
    """
    if mission_id is not None and session_id is not None:
        raise ValueError("Choose either mission_id or session_id when collecting stats")

    session_missions = None
    if session_id is not None:
        session_missions = (
            select(ConversationMessage.mission_id.label("mission_id"))
            .where(
                ConversationMessage.session_id == session_id,
                ConversationMessage.mission_id.is_not(None),
            )
            .union(
                select(ConversationSession.mission_id.label("mission_id")).where(
                    ConversationSession.id == session_id,
                    ConversationSession.mission_id.is_not(None),
                )
            )
            .subquery()
        )

    def scoped(query: Any, column: Any) -> Any:
        if mission_id is not None:
            return query.where(column == mission_id)
        if session_missions is not None:
            return query.where(column.in_(select(session_missions.c.mission_id)))
        return query

    # The missions tab is project history, even when the live usage counters are
    # narrowed to one mission or conversation.
    mission_count = session.scalar(select(func.count(Mission.id))) or 0
    calls_query = select(func.count(ModelCall.id))
    tokens_query = select(
        func.coalesce(func.sum(ModelCall.input_tokens), 0),
        func.coalesce(func.sum(ModelCall.output_tokens), 0),
        func.coalesce(func.sum(ModelCall.estimated_cost), 0.0),
    )
    tools_query = select(func.count(ToolCall.id))
    failures_query = select(func.count(VerificationRun.id)).where(VerificationRun.passed.is_(False))
    calls_query = scoped(calls_query, ModelCall.mission_id)
    tokens_query = scoped(tokens_query, ModelCall.mission_id)
    tools_query = scoped(tools_query, ToolCall.mission_id)
    failures_query = scoped(failures_query, VerificationRun.mission_id)
    calls = session.scalar(calls_query) or 0
    tokens = session.execute(tokens_query).one()
    tool_calls = session.scalar(tools_query) or 0
    failures = session.scalar(failures_query) or 0
    mission_rows = session.scalars(
        scoped(select(Mission).order_by(Mission.created_at), Mission.id)
    ).all()
    task_query = scoped(select(Task), Task.mission_id)
    deployment_query = scoped(select(DeploymentRun), DeploymentRun.mission_id)
    tasks = session.scalars(task_query).all()
    deployments = session.scalars(deployment_query).all()

    def durations(rows: list[Any]) -> list[float]:
        return [max(0.0, (row.updated_at - row.created_at).total_seconds()) for row in rows]

    mission_durations = durations(list(mission_rows))
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
                scoped(
                    select(func.coalesce(func.avg(ModelCall.latency_ms), 0.0)),
                    ModelCall.mission_id,
                )
            )
            or 0
        ),
    }
