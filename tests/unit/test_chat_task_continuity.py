"""A chat session's working memory must survive into the next turn.

The regression this guards: every chat turn opened its own mission, and opening
a mission opened a blank persistent task whose ``original_request`` was the new
message. Typing "continue" therefore produced a task that believed its goal was
the literal word "continue", with no completed steps, no inspected files and no
changed files. The agent had no record of the work it had already done, so it
began the original request again from the start — the user saw a "continue" that
behaved like a fresh run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasuki.application import MissionApplicationService, initialize_project, open_project
from vasuki.memory import PersistentTaskStatus
from vasuki.schemas import TodoItem


@pytest.fixture
def chat(tmp_path: Path) -> MissionApplicationService:
    initialize_project(tmp_path)
    return MissionApplicationService(open_project(tmp_path))


def start_turn(chat: MissionApplicationService, session_id: str, instruction: str):
    """Do what ``chat()`` does before it calls the model, and nothing more."""
    from vasuki.schemas import ProjectMode

    mission = chat.core.create(instruction, ProjectMode.DIRECT, start_task=False)
    chat.attach_session_mission(session_id, mission.id)
    return mission, chat._continue_session_task(session_id, mission.id, instruction)


def test_continue_adopts_the_unfinished_task_instead_of_starting_over(
    chat: MissionApplicationService,
) -> None:
    session_id = chat.create_session("research and build a books page")
    _, first = start_turn(chat, session_id, "research the top 100 books and build a page")
    assert first is not None
    chat.memory.record_action(first.task_id, action="write", paths=["books-data.js"])

    _, second = start_turn(chat, session_id, "continue")

    assert second is not None
    assert second.task_id == first.task_id, "the second turn opened a brand-new task"
    assert second.original_request == "research the top 100 books and build a page"
    assert second.files_changed == ["books-data.js"], "the record of work done was lost"
    assert second.status == PersistentTaskStatus.IN_PROGRESS


def test_continue_after_a_failed_turn_keeps_the_original_goal(
    chat: MissionApplicationService,
) -> None:
    """The reported case: the model's stream died mid-task and the user typed continue."""
    session_id = chat.create_session("books page")
    _, first = start_turn(chat, session_id, "build a top 100 books landing page")
    assert first is not None
    chat.memory.complete_task(first.task_id, summary="stream failed", outcome="failed")

    _, second = start_turn(chat, session_id, "continue")

    assert second is not None
    assert second.task_id == first.task_id
    assert second.original_request == "build a top 100 books landing page"
    assert second.interpreted_goal == "continue"


def test_an_unrelated_question_after_finished_work_starts_a_fresh_task(
    chat: MissionApplicationService,
) -> None:
    """Carrying state forward must not outlive the work it belongs to."""
    session_id = chat.create_session("done and moved on")
    _, first = start_turn(chat, session_id, "rename the header")
    assert first is not None
    chat.memory.complete_task(first.task_id, summary="renamed", outcome="completed")

    _, second = start_turn(chat, session_id, "what does the build script do?")

    assert second is not None
    assert second.task_id != first.task_id
    assert second.original_request == "what does the build script do?"


def test_a_finished_turn_with_steps_outstanding_is_still_continued(
    chat: MissionApplicationService,
) -> None:
    """A turn can end cleanly with the plan unfinished; the plan is what matters."""
    session_id = chat.create_session("multi turn plan")
    _, first = start_turn(chat, session_id, "build the page in stages")
    assert first is not None
    chat.set_session_todos(
        session_id,
        [
            TodoItem(content="write the data file", status="completed"),
            TodoItem(content="write the stylesheet", status="pending"),
        ],
    )
    chat.memory.complete_task(first.task_id, summary="stage one done", outcome="completed")

    _, second = start_turn(chat, session_id, "continue")

    assert second is not None
    assert second.task_id == first.task_id
    assert second.original_request == "build the page in stages"


def test_a_separate_session_does_not_inherit_another_sessions_task(
    chat: MissionApplicationService,
) -> None:
    first_session = chat.create_session("first")
    _, first = start_turn(chat, first_session, "build the page")
    assert first is not None

    second_session = chat.create_session("second")
    _, other = start_turn(chat, second_session, "continue")

    assert other is not None
    assert other.task_id != first.task_id
    assert other.original_request == "continue"


def test_a_question_answered_mid_build_does_not_hijack_the_continuation(
    chat: MissionApplicationService,
) -> None:
    """An ask turn closes its own task; the build it interrupted is still the work."""
    session_id = chat.create_session("build with a question in the middle")
    _, build = start_turn(chat, session_id, "build the top 100 books page")
    assert build is not None
    chat.set_session_todos(session_id, [TodoItem(content="write the data file")])

    aside = chat.memory.start_task(
        "what does index.html do?",
        mission_id=None,
        session_id=session_id,
        status=PersistentTaskStatus.PENDING,
    )
    chat.memory.complete_task(aside.task_id, summary="explained", outcome="completed")

    _, resumed = start_turn(chat, session_id, "continue")

    assert resumed is not None
    assert resumed.task_id == build.task_id
    assert resumed.original_request == "build the top 100 books page"
