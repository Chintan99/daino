"""Chat, provider connection, and responsiveness regressions in the TUI."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from textual.widgets import ContentSwitcher

from daino.application import (
    ProviderApplicationService,
    initialize_project,
    open_project,
)
from daino.events import ModelStreamChunk
from daino.git import GitClient
from daino.model_router import ModelRole
from daino.persistence.models import MissionEventRecord, ModelCall
from daino.tui.app import DainoApp
from daino.tui.screens.workspace import WorkspaceScreen
from daino.tui.widgets import (
    ContextStrip,
    ConversationView,
    NavigationTab,
    NavigationTabs,
    PromptInput,
)
from daino.tui.widgets.message import MessageCard
from daino.tui.widgets.prompt_input import PromptTextArea
from tests.conftest import commit_all, painted_text

ANSWER_WORDS = ("Daino ", "answers ", "the ", "question.")
CATALOG = {
    "data": [
        {
            "id": "vendor/small",
            "name": "Vendor Small",
            "context_length": 128_000,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
        },
        {
            "id": "vendor/large",
            "name": "Vendor Large",
            "context_length": 200_000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
    ]
}


class _Handler(BaseHTTPRequestHandler):
    """Chat-completions double recording every request body it receives."""

    requests: list[dict[str, Any]] = []

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
        if self.path.endswith("/key"):
            self._json({"data": {"label": "test-key", "limit_remaining": 7}})
        elif self.path.endswith("/models"):
            self._json(CATALOG)
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(payload)
        if not payload.get("stream"):
            self._json(
                {
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(self._structured_reply(payload)),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "cost": 0.00000123,
                    },
                }
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # A reasoning observer now makes structured/tool-loop requests stream as
        # well as ordinary answers. Mirror a real Chat Completions server by
        # streaming the schema result as content fragments; returning prose for
        # a JSON-schema request would correctly fail validation in production.
        if any(
            name in payload for name in ("response_format", "format", "guided_json")
        ):
            structured = json.dumps(self._structured_reply(payload))
            midpoint = max(1, len(structured) // 2)
            words = (structured[:midpoint], structured[midpoint:])
        else:
            words = ANSWER_WORDS
        for word in words:
            chunk = {"choices": [{"delta": {"content": word}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "cost": 0.00000123},
        }
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _structured_reply(payload: dict[str, Any]) -> dict[str, Any]:
        """Answer structured requests by the schema name the gateway asked for."""
        schema = payload.get("response_format", {}).get("json_schema", {}).get("name", "")
        body = json.dumps(payload.get("messages", []))
        if schema == "AgentAction" or "AgentAction" in body:
            # The chat agent answers in one turn: it looked, and there is
            # nothing to change, so it responds rather than editing.
            return {
                "thought": "The question needs an answer, not an edit.",
                "action": "respond",
                "message": "".join(ANSWER_WORDS),
            }
        if schema == "RequirementSpec" or "RequirementSpec" in body:
            return {
                "problem_statement": "Add a health endpoint",
                "goals": ["Expose a deterministic health response"],
                "functional_requirements": ["health() returns 200"],
                "acceptance_criteria": ["health() returns (200, 'ok')"],
                "test_strategy": ["python -c 'import health'"],
            }
        return {
            "summary": "Implement the health endpoint",
            "mode": "specification",
            "tasks": [
                {
                    "id": "health",
                    "title": "Add health endpoint",
                    "objective": "Implement health() and verify it",
                    "expected_files": ["health.py"],
                    "allowed_files": ["health.py"],
                    "acceptance_criteria": ["health() returns (200, 'ok')"],
                    "verification_commands": ["python -c 'import health'"],
                }
            ],
        }


@pytest.fixture
def model_server() -> Iterator[str]:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1"
    finally:
        server.shutdown()
        server.server_close()


def connected_app(root: Path, base_url: str = "", **add: str) -> DainoApp:
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    commit_all(root)
    initialize_project(root)
    context = open_project(root)
    if base_url:
        ProviderApplicationService(context).add(
            name=add.get("name", "vendor"),
            provider_type=add.get("provider_type", "openai-compatible"),
            base_url=base_url,
            model=add.get("model", "vendor/small"),
        )
    return DainoApp(root, context=context)


async def ask(pilot: Any, text: str) -> None:
    workspace = pilot.app.screen
    workspace.query_one(PromptInput).focus_prompt()
    await pilot.press(*[character if character != " " else "space" for character in text])
    await pilot.press("enter")


async def wait_for_answer(pilot: Any, attempts: int = 100) -> list[MessageCard]:
    workspace = pilot.app.screen
    for _ in range(attempts):
        await pilot.pause(0.05)
        cards = list(workspace.query(MessageCard))
        if any(card.kind in {"agent", "error"} for card in cards):
            return cards
    return list(workspace.query(MessageCard))


@pytest.mark.asyncio
async def test_chat_answers_once_a_provider_is_connected(
    model_server: str,
    tmp_path: Path,
) -> None:
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        await ask(pilot, "what does app.py do")
        cards = await wait_for_answer(pilot)

        transcript = [(card.kind, card.raw_content) for card in cards]
        assert ("user", "what does app.py do") in transcript
        assert any(
            kind == "agent" and content == "".join(ANSWER_WORDS) for kind, content in transcript
        ), transcript
        # The mission is bookkeeping, not something to narrate at the user.
        assert not any("in direct mode" in content for _, content in transcript)
        # Whether a pure answer claims its bookkeeping mission is a live design
        # question, so it is deliberately not asserted here; what matters is that
        # the turn is not narrated at the user.
        assert [item.kind for item in workspace.missions.messages(workspace.session_id)] == [
            "user",
            "agent",
        ]


@pytest.mark.asyncio
async def test_openrouter_charge_reaches_the_header(model_server: str, tmp_path: Path) -> None:
    app_instance = connected_app(
        tmp_path,
        model_server,
        name="openrouter",
        provider_type="openrouter",
    )
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await ask(pilot, "show the charged usage")
        await wait_for_answer(pilot)

        workspace = app_instance.screen
        for _ in range(40):
            await pilot.pause(0.1)
            usage_text = workspace.query_one("#header-usage").render().plain
            if "$0.00000246" in usage_text:
                break

        with workspace.context.database.session() as session:
            calls = list(session.scalars(select(ModelCall).order_by(ModelCall.created_at.desc())))
            call = calls[0] if calls else None
            assert call is not None
            assert call.estimated_cost == pytest.approx(0.00000123)
        assert "40 tok" in usage_text
        assert "$0.00000246" in usage_text


@pytest.mark.asyncio
async def test_conversation_is_actually_painted_in_the_chat_area(
    model_server: str,
    tmp_path: Path,
) -> None:
    """Guard the whole render path, not just widget state.

    ``MessageCard`` previously defined ``_render_content``, shadowing the Textual
    ``Widget`` method that fills the render cache: cards mounted, reserved space,
    and painted nothing, so the chat area looked permanently empty.
    """
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "DAINO" in painted_text(app_instance)

        await ask(pilot, "render this please")
        await wait_for_answer(pilot)
        await pilot.pause(0.2)

        screen = painted_text(app_instance)
        assert "› render this please" in screen
        assert "".join(ANSWER_WORDS) in screen


@pytest.mark.asyncio
async def test_streamed_markup_characters_do_not_crash_the_render(tmp_path: Path) -> None:
    """Model output is text, never markup.

    Asking for a landing page streams CSS, and a chunk boundary lands mid-token
    on ``button[type="submit`` — an unbalanced ``[`` that no escaper can repair,
    because the bracket only becomes safe once its ``]`` arrives. Textual's
    markup parser raised MarkupError and took the whole app down.
    """
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        chunks = [
            "Here is a complete, single-file landing page:\n",
            "<style>\n",
            ' button[type="submit',
            '"] { color: red }\n',
            "a [bold] b [/] c [#fff] d\\",
            "\n</style>",
        ]
        for chunk in chunks:
            workspace.context.events.publish(
                ModelStreamChunk(mission_id="M-markup", content=chunk, role="summarizer")
            )
            await pilot.pause()

        streamed = [
            card.raw_content for card in workspace.query(MessageCard) if card.kind == "agent"
        ]
        assert streamed == ["".join(chunks)]
        # Rendered verbatim: no tag swallowed, no backslash inserted.
        screen = painted_text(app_instance)
        assert 'button[type="submit"] { color: red }' in screen
        assert "a [bold] b [/] c [#fff] d\\" in screen


@pytest.mark.asyncio
async def test_untrusted_text_in_views_is_never_parsed_as_markup(tmp_path: Path) -> None:
    """Repo paths, mission prompts, and tool summaries all carry brackets."""
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        strip = workspace.query_one("#context-strip", ContextStrip)

        strip.add_activity('12:00:00  ran sed -i "s/[a-z/g" file')
        strip.set_mission("M-1[beta", "running")
        await pilot.pause()

        rendered = painted_text(app_instance)
        assert 'sed -i "s/[a-z/g" file' in rendered
        assert "M-1[beta" in rendered


@pytest.mark.asyncio
async def test_followup_question_carries_conversation_history(
    model_server: str,
    tmp_path: Path,
) -> None:
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        await ask(pilot, "first question")
        await wait_for_answer(pilot)
        await ask(pilot, "second question")
        for _ in range(100):
            await pilot.pause(0.05)
            if len(_Handler.requests) >= 2:
                break

        assert len(_Handler.requests) >= 2
        roles = [message["role"] for message in _Handler.requests[-1]["messages"]]
        assert roles.count("user") >= 2, roles
        assert "assistant" in roles, roles
        assert "first question" in json.dumps(_Handler.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_chat_without_a_provider_explains_how_to_connect_one(tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        await ask(pilot, "hello")
        cards = await wait_for_answer(pilot)

        errors = [card.raw_content for card in cards if card.kind == "error"]
        assert errors, [card.kind for card in cards]
        assert "No model is connected" in errors[0]
        assert "Validate + save" in errors[0]


@pytest.mark.asyncio
async def test_session_model_selection_overrides_routing(
    model_server: str,
    tmp_path: Path,
) -> None:
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        ProviderApplicationService(workspace.context).add(
            name="vendor-large",
            provider_type="openai-compatible",
            base_url=model_server,
            model="vendor/large",
            make_default=False,
        )

        workspace.select_model("vendor-large")
        await ask(pilot, "which model")
        await wait_for_answer(pilot)

        assert _Handler.requests
        assert _Handler.requests[-1]["model"] == "vendor/large"
        assert workspace.active_model == "vendor-large"


@pytest.mark.asyncio
async def test_session_model_selection_also_drives_missions(
    model_server: str,
    tmp_path: Path,
) -> None:
    """A model chosen with Ctrl+M must reach planning, building, and review too."""
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        ProviderApplicationService(workspace.context).add(
            name="vendor-large",
            provider_type="openai-compatible",
            base_url=model_server,
            model="vendor/large",
            make_default=False,
        )
        workspace.select_model("vendor-large")
        core = workspace.missions.core
        pinned = workspace.providers.session_profile(workspace.session_id)
        assert pinned == "vendor-large"

        # Every agent in the mission graph receives the pinned gateway.
        for role in (ModelRole.ARCHITECT, ModelRole.PLANNER, ModelRole.BUILDER):
            assert core._role_available(role, pinned)
        selection = core._gateway(pinned).router.select(
            ModelRole.BUILDER,
            profile_override=pinned,
        )
        assert selection.profile.model == "vendor/large"
        assert core._gateway(pinned).profile_override == "vendor-large"
        # Without a selection the configured routing still applies.
        assert core._gateway("") is core.gateway


@pytest.mark.asyncio
async def test_planning_uses_the_selected_model(model_server: str, tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path, model_server)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        ProviderApplicationService(workspace.context).add(
            name="vendor-large",
            provider_type="openai-compatible",
            base_url=model_server,
            model="vendor/large",
            make_default=False,
        )
        workspace.select_model("vendor-large")
        _Handler.requests = []

        worker = workspace.plan_mission("Add a health endpoint.")
        for _ in range(200):
            await pilot.pause(0.05)
            if worker.is_finished:
                break
        await pilot.pause(0.3)

        assert workspace.active_mission_id, "planning never produced a mission"
        assert _Handler.requests, "planning never called the model"
        # Requirements compilation and planning both ran on the pinned profile.
        assert {request["model"] for request in _Handler.requests} == {"vendor/large"}
        assert len(_Handler.requests) >= 2
        details = workspace.missions.mission_details(workspace.active_mission_id)
        assert [task["title"] for task in details["tasks"]] == ["Add health endpoint"]


@pytest.mark.asyncio
async def test_streaming_does_not_shell_out_to_git_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        calls: list[str] = []
        original = GitClient.current_branch
        monkeypatch.setattr(
            GitClient,
            "current_branch",
            lambda self: (calls.append("branch"), original(self))[1],
        )

        for index in range(60):
            workspace.context.events.publish(
                ModelStreamChunk(mission_id="M-stream", content=f"{index} ", role="builder")
            )
        await pilot.pause()
        await pilot.pause(0.4)

        conversation = workspace.query_one("#chat-view", ConversationView)
        streamed = [
            card.raw_content for card in conversation.query(MessageCard) if card.kind == "agent"
        ]
        assert streamed and streamed[0].startswith("0 1 2 ")
        # One coalesced refresh may run; sixty must not.
        assert len(calls) <= 2, len(calls)


@pytest.mark.asyncio
async def test_stream_chunks_are_not_written_to_the_event_log(tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        for index in range(25):
            workspace.context.events.publish(
                ModelStreamChunk(mission_id="M-log", content=str(index))
            )
        await pilot.pause()

        with workspace.context.database.session() as session:
            stored = session.scalar(
                select(func.count())
                .select_from(MissionEventRecord)
                .where(MissionEventRecord.kind == "ModelStreamChunk")
            )
        assert stored == 0


@pytest.mark.asyncio
async def test_opening_a_view_by_command_moves_the_tab_highlight(tmp_path: Path) -> None:
    app_instance = connected_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        tabs = workspace.query_one("#nav-tabs", NavigationTabs)
        switcher = workspace.query_one("#main-workspace", ContentSwitcher)

        await workspace.execute_command("/logs")
        await pilot.pause()

        assert switcher.current == "logs-view"
        assert [tab.view_id for tab in tabs.query(NavigationTab) if tab.active] == ["logs-view"]

        # Views with no tab clear the highlight rather than lying about chat.
        await workspace.execute_command("/settings")
        await pilot.pause()
        assert switcher.current == "settings-view"
        assert [tab.view_id for tab in tabs.query(NavigationTab) if tab.active] == []


@pytest.mark.asyncio
async def test_enter_sends_and_shift_enter_makes_a_newline(tmp_path: Path) -> None:
    """Enter is the send key, and it is the only one.

    It used to be configurable, which meant a project could end up bound to
    ctrl+enter and every prompt needed two hands for no reason.
    """
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    app_instance = DainoApp(tmp_path, context=context)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        prompt = workspace.query_one(PromptInput).query_one("#prompt", PromptTextArea)

        assert prompt.submit_keys == {"enter"}
        assert "ctrl+enter" not in prompt.submit_keys
        assert "enter" not in prompt.newline_keys

        # Shift+Enter keeps typing; Enter sends what was typed.
        prompt.focus()
        await pilot.press("h", "i", "shift+enter", "y", "o")
        await pilot.pause()
        assert prompt.text == "hi\nyo"

        await pilot.press("enter")
        await pilot.pause()
        assert prompt.text == ""
        assert any(card.raw_content == "hi\nyo" for card in workspace.query(MessageCard))


def test_the_submit_shortcut_is_no_longer_configurable() -> None:
    """Removed rather than defaulted, so it cannot drift back to ctrl+enter."""
    from daino.config.models import TUIConfig

    assert "submit_shortcut" not in TUIConfig.model_fields
