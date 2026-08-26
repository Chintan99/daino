"""The /team slash command, end to end against a real provider and workspace.

Unlike the unit tests, nothing here is a gateway double: the request travels
through the real ``ModelGateway``, provider adapter, ``ToolLoop``, and
``EditTools``, and the members write into a real Git worktree.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from daino.application import ProviderApplicationService, initialize_project, open_project
from daino.tui.app import DainoApp
from daino.tui.keybindings import SLASH_COMMANDS
from daino.tui.screens.workspace import WorkspaceScreen
from tests.conftest import commit_all

#: Two members with disjoint scopes, so validation lets them run in one wave.
TEAM_PLAN = {
    "summary": "Split the work by subsystem",
    "members": [
        {
            "id": "api-writer",
            "role": "builder",
            "objective": "Create api/out.txt containing the word api.",
            "scope": ["api/**"],
            "read_only": False,
            "dependencies": [],
        },
        {
            "id": "web-writer",
            "role": "builder",
            "objective": "Create web/out.txt containing the word web.",
            "scope": ["web/**"],
            "read_only": False,
            "dependencies": [],
        },
    ],
}


class _Handler(BaseHTTPRequestHandler):
    """Answers the team lead once, then scripts each member's loop."""

    lock = threading.Lock()
    turns: dict[str, int] = {}
    peak_concurrent = 0
    concurrent = 0

    def log_message(self, *args: object) -> None:
        return

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._json({"data": []})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        reply = self._reply(json.dumps(payload.get("messages", [])))
        self._json(
            {
                "model": payload.get("model"),
                "choices": [
                    {
                        "message": {"role": "assistant", "content": json.dumps(reply)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

    def _reply(self, body: str) -> dict[str, Any]:
        if "Team Lead" in body:
            return TEAM_PLAN
        target = "api/out.txt" if "api/out.txt" in body else "web/out.txt"
        cls = type(self)
        with cls.lock:
            cls.concurrent += 1
            cls.peak_concurrent = max(cls.peak_concurrent, cls.concurrent)
            turn = cls.turns.get(target, 0)
            cls.turns[target] = turn + 1
        try:
            # Hold the window open long enough that a genuinely parallel peer is
            # observed inside it; without this the counter proves nothing.
            time.sleep(0.2)
            with cls.lock:
                cls.peak_concurrent = max(cls.peak_concurrent, cls.concurrent)
            if turn == 0:
                return {
                    "thought": "write the file",
                    "action": "write",
                    "path": target,
                    "content": target.split("/")[0],
                }
            return {
                "thought": "done",
                "action": "finish",
                "summary": f"wrote {target}",
                "verification_commands": [],
            }
        finally:
            with cls.lock:
                cls.concurrent -= 1


@pytest.fixture
def team_server() -> Iterator[str]:
    _Handler.turns = {}
    _Handler.peak_concurrent = 0
    _Handler.concurrent = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1"
    finally:
        server.shutdown()
        server.server_close()


def connected_app(root: Path, base_url: str) -> DainoApp:
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
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


def test_team_is_a_registered_slash_command() -> None:
    entry = next((item for item in SLASH_COMMANDS if item.name == "/team"), None)
    assert entry is not None
    assert entry.usage == "<instruction>"


@pytest.mark.asyncio
async def test_team_command_runs_members_and_writes_their_files(
    team_server: str, tmp_path: Path
) -> None:
    app_instance = connected_app(tmp_path, team_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("/team split the work by subsystem")
        for _ in range(200):
            await pilot.pause(0.05)
            if workspace.active_status in {"Ready", "Failed"}:
                break

        assert workspace.active_status == "Ready", workspace.active_status
        assert workspace.active_mission_id

        kinds = [item.kind for item in workspace.missions.messages(workspace.session_id)]
        assert kinds == ["user", "summary", "summary"]
        transcript = "\n".join(
            item.content for item in workspace.missions.messages(workspace.session_id)
        )
        # The roster is shown before the work, with its wave structure.
        assert "Wave 1 (2 members in parallel)" in transcript
        assert "api-writer [builder]" in transcript
        assert "web-writer [builder]" in transcript
        # And the result names both files.
        assert "api/out.txt" in transcript
        assert "web/out.txt" in transcript

        details = workspace.missions.mission_details(workspace.active_mission_id)
        worktree = Path(details["mission"]["workspace_path"])
        assert (worktree / "api/out.txt").read_text(encoding="utf-8") == "api"
        assert (worktree / "web/out.txt").read_text(encoding="utf-8") == "web"
        # The two members were genuinely in flight at the same time.
        assert _Handler.peak_concurrent == 2


@pytest.mark.asyncio
async def test_team_without_an_instruction_explains_the_usage(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    app_instance = DainoApp(tmp_path, context=context)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        notices: list[str] = []
        workspace.notify = lambda message, **kwargs: notices.append(str(message))  # type: ignore[method-assign]
        await workspace.execute_command("/team")

        assert notices == ["Usage: /team <instruction>"]
        assert workspace.missions.messages(workspace.session_id) == []
