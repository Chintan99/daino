from __future__ import annotations

from pathlib import Path

import pytest

from vasuki.observability import collect_stats
from vasuki.persistence import Database
from vasuki.persistence.models import (
    ConversationMessage,
    ConversationSession,
    Mission,
    ModelCall,
)


def _mission(project_id: str, mission_id: str) -> Mission:
    return Mission(
        id=mission_id,
        project_id=project_id,
        request=mission_id,
        mode="direct",
        status="completed",
        workspace_path=None,
        branch=None,
        initial_revision=None,
        final_revision=None,
        failure=None,
    )


def _call(
    mission_id: str,
    call_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost: float,
) -> ModelCall:
    return ModelCall(
        id=call_id,
        mission_id=mission_id,
        role="builder",
        provider="test-provider",
        model="test-model",
        selection_reason="test",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=10,
        estimated_cost=cost,
        success=True,
    )


def test_conversation_stats_include_only_missions_linked_to_that_session(
    project: tuple[Path, object, Database],
) -> None:
    _, _, database = project
    project_id = database.project().id
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-prior-turn"),
                _mission(project_id, "mission-current-turn"),
                _mission(project_id, "mission-other-window"),
                ConversationSession(
                    id="session-current",
                    project_id=project_id,
                    mission_id="mission-current-turn",
                    title="current",
                ),
                ConversationSession(
                    id="session-other",
                    project_id=project_id,
                    mission_id="mission-other-window",
                    title="other",
                ),
                ConversationMessage(
                    id="message-prior",
                    session_id="session-current",
                    mission_id="mission-prior-turn",
                    kind="agent",
                    role="builder",
                    content="prior reply",
                ),
                _call(
                    "mission-prior-turn",
                    "call-prior",
                    input_tokens=10,
                    output_tokens=5,
                    cost=0.01,
                ),
                _call(
                    "mission-current-turn",
                    "call-current",
                    input_tokens=20,
                    output_tokens=7,
                    cost=0.02,
                ),
                _call(
                    "mission-other-window",
                    "call-other",
                    input_tokens=1_000,
                    output_tokens=500,
                    cost=3.0,
                ),
            ]
        )

    with database.session() as session:
        stats = collect_stats(session, session_id="session-current")

    assert stats["model_calls"] == 2
    assert stats["input_tokens"] == 30
    assert stats["output_tokens"] == 12
    assert stats["estimated_cost"] == pytest.approx(0.03)


def test_fresh_conversation_does_not_inherit_project_usage(
    project: tuple[Path, object, Database],
) -> None:
    _, _, database = project
    project_id = database.project().id
    with database.session() as session:
        session.add_all(
            [
                _mission(project_id, "mission-old"),
                ConversationSession(
                    id="session-old",
                    project_id=project_id,
                    mission_id="mission-old",
                    title="old",
                ),
                ConversationSession(
                    id="session-fresh",
                    project_id=project_id,
                    mission_id=None,
                    title="fresh",
                ),
                _call(
                    "mission-old",
                    "call-old",
                    input_tokens=100,
                    output_tokens=50,
                    cost=1.25,
                ),
            ]
        )

    with database.session() as session:
        stats = collect_stats(session, session_id="session-fresh")

    assert stats["model_calls"] == 0
    assert stats["input_tokens"] == 0
    assert stats["output_tokens"] == 0
    assert stats["estimated_cost"] == 0


def test_stats_reject_ambiguous_mission_and_conversation_scope(
    project: tuple[Path, object, Database],
) -> None:
    _, _, database = project
    with database.session() as session:
        with pytest.raises(ValueError, match="either mission_id or session_id"):
            collect_stats(session, "mission-1", session_id="session-1")
