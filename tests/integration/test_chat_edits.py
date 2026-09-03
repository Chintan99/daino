"""A bare chat prompt must edit the repository, not describe the change.

The regression this guards: a plain instruction used to be routed to the
question-answering path, which has no editor, so the model could only reply with
a code block while the file on disk stayed exactly as it was.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from daino.application import ProviderApplicationService, initialize_project, open_project
from daino.tui.app import DainoApp
from daino.tui.screens.workspace import WorkspaceScreen
from daino.tui.widgets.message import MessageCard, _diff_marker
from tests.conftest import commit_all


def _is_change(line: str) -> bool:
    return _diff_marker(line) in {"+", "-"}


ORIGINAL = "<!DOCTYPE html>\n<html>\n<body>\n<h1>Welcome</h1>\n</body>\n</html>\n"
#: Passes the first time it runs and fails every time after, so a turn that
#: verifies twice sees the second result. ``sys.executable`` rather than
#: "python": that name does not exist on modern macOS or most Linux distros, so
#: the command failed with "not found" both times and the check was never
#: actually flaky.
FLAKY_CHECK = (
    f'{shlex.quote(sys.executable)} -c "from pathlib import Path; '
    "p=Path('.vasuki-flaky-check'); "
    "first=not p.exists(); p.write_text('seen'); raise SystemExit(0 if first else 1)\""
)


class _Handler(BaseHTTPRequestHandler):
    """Scripts a read-then-edit-then-finish turn, or a plain answer."""

    #: Set per test: "edit" or "answer".
    mode = "edit"
    turns = 0
    bodies: list[list[dict[str, Any]]] = []

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._json({"data": []})

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        cls.turns += 1
        cls.bodies.append(payload.get("messages", []))
        self._json(
            {
                "model": payload.get("model"),
                "choices": [
                    {
                        "message": {"role": "assistant", "content": json.dumps(self._action())},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

    def _action(self) -> dict[str, Any]:
        cls = type(self)
        if cls.mode == "stall":
            # Read, one edit that lands, then the same replace over and over with
            # an old_string that no longer exists — exactly the observed stall.
            if cls.turns == 1:
                return {"thought": "read it", "action": "read_file", "path": "landing.html"}
            if cls.turns == 2:
                return {
                    "thought": "apply it",
                    "action": "replace",
                    "path": "landing.html",
                    "old_string": "<h1>Welcome</h1>",
                    "new_string": '<h1 class="glass">Welcome</h1>',
                }
            return {
                "thought": "and again",
                "action": "replace",
                "path": "landing.html",
                "old_string": "<h1>Welcome</h1>",
                "new_string": '<h1 class="dark">Welcome</h1>',
            }
        if cls.mode == "answer":
            return {
                "thought": "this is a question",
                "action": "respond",
                "message": "landing.html renders a single welcome heading.",
            }
        if cls.turns == 1:
            return {"thought": "read it first", "action": "read_file", "path": "landing.html"}
        if cls.turns == 2:
            return {
                "thought": "apply the theme",
                "action": "replace",
                "path": "landing.html",
                "old_string": "<h1>Welcome</h1>",
                "new_string": '<h1 class="glass">Welcome</h1>',
            }
        if cls.turns == 3:
            return {
                "thought": "verify the edit",
                "action": "run_command",
                "command": FLAKY_CHECK if cls.mode == "flaky" else "git diff --check",
            }
        return {
            "thought": "done",
            "action": "finish",
            "summary": "Applied the glassmorphism heading style.",
            "verification_commands": [FLAKY_CHECK if cls.mode == "flaky" else "git diff --check"],
        }


@pytest.fixture
def agent_server() -> Iterator[str]:
    _Handler.turns = 0
    _Handler.mode = "edit"
    _Handler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1"
    finally:
        server.shutdown()
        server.server_close()


def connected_app(root: Path, base_url: str) -> DainoApp:
    (root / "landing.html").write_text(ORIGINAL, encoding="utf-8")
    commit_all(root)
    initialize_project(root)
    context = open_project(root)
    ProviderApplicationService(context).add(
        name="vendor",
        provider_type="openai-compatible",
        base_url=base_url,
        model="vendor/small",
    )
    return DainoApp(root, context=context)


async def settle(pilot: Any, workspace: WorkspaceScreen, attempts: int = 200) -> None:
    for _ in range(attempts):
        await pilot.pause(0.05)
        if workspace.active_status in {"Ready", "Failed"}:
            return


@pytest.mark.asyncio
async def test_a_bare_instruction_edits_the_file_on_disk(agent_server: str, tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        # The file actually changed. This is the whole point.
        text = (tmp_path / "landing.html").read_text(encoding="utf-8")
        assert '<h1 class="glass">Welcome</h1>' in text
        assert text != ORIGINAL


@pytest.mark.asyncio
async def test_the_transcript_shows_a_diff_rather_than_a_code_dump(
    agent_server: str, tmp_path: Path
) -> None:
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        items = workspace.missions.messages(workspace.session_id)
        kinds = [item.kind for item in items]
        # The per-edit diff lands while the turn runs; the changeset closes it.
        assert kinds == ["user", "diff", "tool", "test", "agent", "changeset"]

        changeset = items[-1]
        assert changeset.content.splitlines()[0] == "Edited 1 file  +1 -1"
        assert changeset.metadata["files"] == [
            {"path": "landing.html", "change": "modified", "added": 1, "removed": 1}
        ]

        diff = next(item for item in items if item.kind == "diff")
        assert diff.content.splitlines()[0] == "landing.html"
        assert "Added 1 line, removed 1 line" in diff.content
        assert "- <h1>Welcome</h1>" in diff.content
        assert '+ <h1 class="glass">Welcome</h1>' in diff.content
        # Exactly one line added and one removed; the rest is context, not a
        # re-paste of the file as prose.
        changed = [line for line in diff.content.splitlines() if _is_change(line)]
        assert len(changed) == 2
        assert "```" not in diff.content

        # And the diff is a real card in the chat area.
        cards = [card for card in workspace.query(MessageCard) if card.kind == "diff"]
        assert len(cards) == 1


@pytest.mark.asyncio
async def test_a_question_is_answered_without_touching_the_repository(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.mode = "answer"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("what does landing.html do?")
        await settle(pilot, workspace)

        assert (tmp_path / "landing.html").read_text(encoding="utf-8") == ORIGINAL
        items = workspace.missions.messages(workspace.session_id)
        assert [item.kind for item in items] == ["user", "agent"]
        assert "single welcome heading" in items[-1].content


@pytest.mark.asyncio
async def test_a_checkpoint_is_taken_before_the_agent_edits(
    agent_server: str, tmp_path: Path
) -> None:
    """The agent writes to the real tree, so there must be a way back."""
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        descriptions = [item.description for item in workspace.checkpoints.list()]
        assert "Before chat edit" in descriptions


@pytest.mark.asyncio
async def test_failed_final_verification_does_not_report_the_chat_mission_complete(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.mode = "flaky"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        assert workspace.active_status == "Failed"
        mission = workspace.missions.list_missions(1)[0]
        details = workspace.missions.mission_details(mission.id)
        assert details["mission"]["status"] == "failed"
        messages = workspace.missions.messages(workspace.session_id)
        assert any(
            item.kind == "error" and "task is not complete" in item.content for item in messages
        )


@pytest.mark.asyncio
async def test_a_pure_answer_does_not_claim_the_active_mission(
    agent_server: str, tmp_path: Path
) -> None:
    """A question opens an audit-only mission; it must not steal /diff or /review."""
    _Handler.mode = "answer"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace.active_mission_id = "mission-in-progress"

        await workspace.execute_command("what does landing.html do?")
        await settle(pilot, workspace)

        assert workspace.active_mission_id == "mission-in-progress"


@pytest.mark.asyncio
async def test_an_edit_turn_becomes_the_active_mission(agent_server: str, tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        assert workspace.active_mission_id
        assert workspace.active_mission_id != "mission-in-progress"


@pytest.mark.asyncio
async def test_a_followup_turn_carries_the_earlier_conversation(
    agent_server: str, tmp_path: Path
) -> None:
    """Without history the agent cannot resolve "now do the same for the footer"."""
    _Handler.mode = "answer"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("what does landing.html do?")
        await settle(pilot, workspace)
        _Handler.bodies.clear()
        await workspace.execute_command("and the footer?")
        await settle(pilot, workspace)

        assert _Handler.bodies, "the follow-up never reached the model"
        roles = [message["role"] for message in _Handler.bodies[-1]]
        assert roles.count("user") >= 2, roles
        assert "assistant" in roles, roles
        assert "what does landing.html do?" in json.dumps(_Handler.bodies[-1])


@pytest.mark.asyncio
async def test_each_edit_posts_its_own_diff_as_it_lands(agent_server: str, tmp_path: Path) -> None:
    """A bare "Replace landing.html" line says nothing about what the agent did."""
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        cards = list(workspace.query(MessageCard))
        # No bare tool card announcing the path with no content.
        assert not [
            card for card in cards if card.kind == "tool" and "landing.html" in card.raw_content
        ]
        diffs = [card for card in cards if card.kind == "diff"]
        assert len(diffs) == 1
        assert "- <h1>Welcome</h1>" in diffs[0].raw_content


@pytest.mark.asyncio
async def test_a_created_file_is_summarized_not_pasted_back(tmp_path: Path) -> None:
    """Every line of a new file is an addition; rendering them all is just the file."""
    from daino.tools.diffing import MAX_CREATED_LINES, build_file_diff, render

    body = "\n".join(f"<div>line {index}</div>" for index in range(200)) + "\n"
    diff = build_file_diff("cars.html", None, body)
    text = render(diff)

    assert diff.change == "created"
    assert diff.added == 200
    # The counts are reported in full...
    assert "Created with 200 lines" in text
    # ...but only a short head is rendered.
    assert len([line for line in text.splitlines() if _is_change(line)]) == MAX_CREATED_LINES
    assert "Showing the first 20 of 200 lines." in text


@pytest.mark.asyncio
async def test_a_fresh_session_sends_no_history_from_last_time(
    agent_server: str, tmp_path: Path
) -> None:
    """The point of not resuming: yesterday's transcript is not re-sent, or paid for."""
    _Handler.mode = "answer"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("first ever question")
        await settle(pilot, workspace)

    # A second launch against the same project.
    from daino.application import open_project

    second = DainoApp(tmp_path, context=open_project(tmp_path))
    async with second.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = second.screen
        assert isinstance(workspace, WorkspaceScreen)
        _Handler.bodies.clear()

        await workspace.execute_command("a brand new question")
        await settle(pilot, workspace)

        assert _Handler.bodies, "the prompt never reached the model"
        sent = json.dumps(_Handler.bodies[-1])
        assert "a brand new question" in sent
        assert "first ever question" not in sent


@pytest.mark.asyncio
async def test_the_pre_edit_checkpoint_is_not_announced_every_turn(
    agent_server: str, tmp_path: Path
) -> None:
    """The safety checkpoint is kept; the card announcing it on every prompt is not."""
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("make the heading glassmorphism")
        await settle(pilot, workspace)

        kinds = [item.kind for item in workspace.missions.messages(workspace.session_id)]
        assert "checkpoint" not in kinds
        assert not [card for card in workspace.query(MessageCard) if card.kind == "checkpoint"]
        # The checkpoint itself still exists, so /restore still works.
        assert "Before chat edit" in [item.description for item in workspace.checkpoints.list()]


@pytest.mark.asyncio
async def test_a_turn_that_stops_early_still_reports_what_it_changed(
    agent_server: str, tmp_path: Path
) -> None:
    """The regression from ~/vasukitest/project4: partial work reported as nothing.

    The agent edited the file, then failed three replaces in a row and hit the
    no-progress guard. The transcript showed a bare red failure, so the edits
    already sitting in the working tree were invisible.
    """
    _Handler.mode = "stall"
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("add a dark theme")
        await settle(pilot, workspace)

        items = workspace.missions.messages(workspace.session_id)
        kinds = [item.kind for item in items]
        assert "error" in kinds, kinds

        # The edit that landed is summarised, not silently dropped.
        changesets = [item for item in items if item.kind == "changeset"]
        assert changesets, f"no changeset recorded; kinds were {kinds}"
        assert changesets[0].metadata["files"] == [
            {"path": "landing.html", "change": "modified", "added": 1, "removed": 1}
        ]

        # And the failure itself names the file, so the user knows to look.
        failures = [item for item in items if item.kind == "error"]
        assert failures, f"no failure recorded; kinds were {kinds}"
        assert "had already been changed and were kept" in failures[-1].content
        assert "landing.html" in failures[-1].content
