"""The agent's shell as the user experiences it: visible work, and a real prompt.

Two failures these guard against, both of which make a working agent look broken:
a command that runs with nothing shown in the transcript, and an approval that
never reaches the screen because it was requested from inside a worker.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from daino.application import ProviderApplicationService, initialize_project, open_project
from daino.schemas import InteractionMode
from daino.tools.web import WebResearchTool
from daino.tui.app import DainoApp
from daino.tui.screens.workspace import WorkspaceScreen
from daino.tui.widgets import ApprovalModal, ContextStrip, TaskChecklist
from tests.conftest import commit_all, painted_text


class _Handler(BaseHTTPRequestHandler):
    """Replays a fixed action script, one action per request."""

    script: list[dict[str, Any]] = []
    turns = 0

    def log_message(self, *args: object) -> None:
        return

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._json({"data": []})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        cls = type(self)
        action = cls.script[min(cls.turns, len(cls.script) - 1)]
        cls.turns += 1
        self._json(
            {
                "model": "m",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": json.dumps(action)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        )


FINISH = {"thought": "t", "action": "finish", "summary": "done", "verification_commands": []}


@pytest.fixture
def agent_server() -> Iterator[str]:
    _Handler.turns = 0
    _Handler.script = [FINISH]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1"
    finally:
        server.shutdown()
        server.server_close()


def connected_app(root: Path, base_url: str) -> DainoApp:
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(root)
    initialize_project(root)
    context = open_project(root)
    # The agent runs commands in the project runtime; Docker is not assumed here.
    context.settings.runtime.default = "local"
    ProviderApplicationService(context).add(
        name="vendor",
        provider_type="openai-compatible",
        base_url=base_url,
        model="vendor/small",
    )
    return DainoApp(root, context=context)


async def settle(pilot: Any, workspace: WorkspaceScreen, attempts: int = 300) -> None:
    for _ in range(attempts):
        await pilot.pause(0.05)
        if workspace.active_status in {"Ready", "Failed"}:
            return


@pytest.mark.asyncio
async def test_a_command_and_its_output_appear_in_the_transcript(
    agent_server: str, tmp_path: Path
) -> None:
    """A command that runs invisibly leaves the user unable to tell what happened."""
    _Handler.script = [
        {"thought": "t", "action": "run_command", "command": "python3 --version"},
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("what python is this")
        await settle(pilot, workspace)

        items = workspace.missions.messages(workspace.session_id)
        tools = [item for item in items if item.kind == "tool"]
        assert tools, [item.kind for item in items]
        assert tools[0].content.startswith("$ python3 --version")
        assert "Python 3" in tools[0].content


@pytest.mark.asyncio
async def test_a_failed_command_is_shown_as_an_error_and_the_run_continues(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.script = [
        {"thought": "t", "action": "run_command", "command": "python3 -c import~nonsense"},
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("run something broken")
        await settle(pilot, workspace)

        items = workspace.missions.messages(workspace.session_id)
        kinds = [item.kind for item in items]
        assert "error" in kinds
        # The failure did not abort the turn: the agent still finished.
        assert kinds[-1] == "agent"
        assert workspace.active_status == "Ready"


@pytest.mark.asyncio
async def test_a_crashed_chat_turn_cannot_return_the_status_to_ready(
    agent_server: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        async def crash(*args: object, **kwargs: object) -> object:
            raise RuntimeError("agent loop crashed")

        monkeypatch.setattr(workspace.missions, "chat", crash)
        await workspace.execute_command("change the application")
        await settle(pilot, workspace)

        assert workspace.active_status == "Failed"
        assert any(
            "agent loop crashed" in item.content
            for item in workspace.missions.messages(workspace.session_id)
        ) or "agent loop crashed" in painted_text(app_instance)


@pytest.mark.asyncio
async def test_a_gated_command_actually_reaches_the_screen(
    agent_server: str, tmp_path: Path
) -> None:
    """Approval is requested from inside a worker; it must still show a modal."""
    _Handler.script = [
        {"thought": "t", "action": "run_command", "command": "pip install --dry-run httpx"},
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("install httpx")
        shown = False
        for _ in range(300):
            await pilot.pause(0.05)
            if any(isinstance(screen, ApprovalModal) for screen in app_instance.screen_stack):
                shown = True
                await pilot.press("r")
            if workspace.active_status in {"Ready", "Failed"}:
                break

        assert shown, "an install ran without ever asking the user"
        # Declining is a normal outcome, not a crash.
        assert workspace.active_status == "Ready"
        contents = [item.content for item in workspace.missions.messages(workspace.session_id)]
        assert any("pip install --dry-run httpx" in text for text in contents)


@pytest.mark.asyncio
async def test_a_safe_command_never_interrupts_the_user(agent_server: str, tmp_path: Path) -> None:
    _Handler.script = [
        {"thought": "t", "action": "run_command", "command": "git status"},
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("what changed")
        seen_modal = False
        for _ in range(300):
            await pilot.pause(0.05)
            if any(isinstance(screen, ApprovalModal) for screen in app_instance.screen_stack):
                seen_modal = True
                break
            if workspace.active_status in {"Ready", "Failed"}:
                break

        assert not seen_modal
        assert workspace.active_status == "Ready"


@pytest.mark.asyncio
async def test_the_plan_is_shown_when_the_agent_makes_one(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.script = [
        {
            "thought": "t",
            "action": "todo",
            "todos": [
                {"content": "read the file", "status": "completed"},
                {"content": "change it", "status": "in_progress"},
            ],
        },
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("do a few things")
        await settle(pilot, workspace)

        contents = "\n".join(
            item.content for item in workspace.missions.messages(workspace.session_id)
        )
        assert "[x] read the file" in contents
        assert "[>] change it" in contents
        checklist = workspace.query_one("#task-checklist", TaskChecklist)
        assert checklist.display
        assert [item.status for item in checklist.todos] == ["completed", "in_progress"]


@pytest.mark.asyncio
async def test_session_mode_runs_gated_commands_without_an_approval_modal(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.script = [
        {"thought": "t", "action": "run_command", "command": "pip --version"},
        FINISH,
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace._set_interaction_mode(InteractionMode.SESSION)

        await workspace.execute_command("check pip")
        seen_modal = False
        for _ in range(300):
            await pilot.pause(0.05)
            seen_modal |= any(
                isinstance(screen, ApprovalModal) for screen in app_instance.screen_stack
            )
            if workspace.active_status in {"Ready", "Failed"}:
                break

        assert not seen_modal
        assert workspace.active_status == "Ready"


@pytest.mark.asyncio
async def test_session_mode_researches_the_web_and_shows_sources(
    agent_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Handler.script = [
        {
            "thought": "find current documentation",
            "action": "web_search",
            "query": "Python current documentation",
            "max_results": 3,
        },
        {
            "thought": "answer from the source",
            "action": "respond",
            "message": "The current documentation is available from the official Python site: "
            "https://docs.python.org/3/",
        },
    ]

    async def fake_get(
        self: WebResearchTool,
        url: str,
        *,
        params: dict[str, str] | None = None,
        method: str = "GET",
        data: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, str, str]:
        return (
            url,
            '<a class="result__a" href="https://docs.python.org/3/">Python docs</a>',
            "text/html",
        )

    monkeypatch.setattr(WebResearchTool, "_get", fake_get)
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace._set_interaction_mode(InteractionMode.SESSION)

        await workspace.execute_command("research current Python documentation")
        await settle(pilot, workspace)

        items = workspace.missions.messages(workspace.session_id)
        tool_messages = [item.content for item in items if item.kind == "tool"]
        assert any("web search Python current documentation" in item for item in tool_messages)
        assert any("https://docs.python.org/3/" in item for item in tool_messages)
        assert any(
            item.kind == "agent" and "https://docs.python.org/3/" in item.content for item in items
        )


@pytest.mark.asyncio
async def test_session_approval_is_reused_by_post_edit_verification(
    agent_server: str, tmp_path: Path
) -> None:
    _Handler.script = [
        {
            "thought": "plan",
            "action": "todo",
            "todos": [{"content": "Run verification", "status": "pending"}],
        },
        {"thought": "read", "action": "read_file", "path": "a.py"},
        {
            "thought": "edit",
            "action": "replace",
            "path": "a.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
        },
        {"thought": "verify", "action": "run_command", "command": "/usr/bin/true"},
        {
            "thought": "done",
            "action": "finish",
            "summary": "updated x",
            "verification_commands": ["/usr/bin/true"],
        },
    ]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace._set_interaction_mode(InteractionMode.SESSION)

        await workspace.execute_command("change x")
        await settle(pilot, workspace)

        assert workspace.active_status == "Ready"
        assert workspace.active_mission_id
        details = workspace.missions.mission_details(workspace.active_mission_id)
        assert details["mission"]["status"] == "completed"
        assert details["tests"] and details["tests"][-1]["passed"]
        assert workspace.query_one("#context-strip", ContextStrip).verification != "running…"
        checklist = workspace.query_one("#task-checklist", TaskChecklist)
        assert [item.status for item in checklist.todos] == ["completed"]


@pytest.mark.asyncio
async def test_bang_runs_a_shell_command_instead_of_prompting_the_model(
    agent_server: str, tmp_path: Path
) -> None:
    """A leading ! is the user running something, not asking the agent to."""
    _Handler.script = [FINISH]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("!echo hello-from-shell")
        for _ in range(200):
            await pilot.pause(0.05)
            items = workspace.missions.messages(workspace.session_id)
            if any(item.kind in {"tool", "error"} for item in items):
                break

        items = workspace.missions.messages(workspace.session_id)
        kinds = [item.kind for item in items]
        assert kinds == ["user", "tool"], kinds
        assert items[0].content == "! echo hello-from-shell"
        assert "hello-from-shell" in items[1].content
        # The model was never consulted.
        assert _Handler.turns == 0


@pytest.mark.asyncio
async def test_bang_supports_real_shell_syntax(agent_server: str, tmp_path: Path) -> None:
    """Pipes and globs are the point; the agent's shell-free exec cannot do them."""
    _Handler.script = [FINISH]
    (tmp_path / "one.txt").write_text("a\nb\nc\n", encoding="utf-8")
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("!cat *.txt | wc -l")
        for _ in range(200):
            await pilot.pause(0.05)
            if len(workspace.missions.messages(workspace.session_id)) >= 2:
                break

        items = workspace.missions.messages(workspace.session_id)
        assert items[-1].kind == "tool"
        assert "3" in items[-1].content


@pytest.mark.asyncio
async def test_a_failing_bang_command_shows_its_error(agent_server: str, tmp_path: Path) -> None:
    _Handler.script = [FINISH]
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await workspace.execute_command("!definitely-not-a-command")
        for _ in range(200):
            await pilot.pause(0.05)
            if len(workspace.missions.messages(workspace.session_id)) >= 2:
                break

        items = workspace.missions.messages(workspace.session_id)
        assert items[-1].kind == "error"
        assert "not found" in items[-1].content


@pytest.mark.asyncio
async def test_a_bare_bang_explains_the_usage(agent_server: str, tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path, agent_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        notices: list[str] = []
        workspace.notify = lambda message, **kw: notices.append(str(message))  # type: ignore[method-assign]

        await workspace.execute_command("!")

        assert notices and "!<command>" in notices[0]
        assert workspace.missions.messages(workspace.session_id) == []
