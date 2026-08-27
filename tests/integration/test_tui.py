from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual import events as textual_events
from textual.containers import VerticalScroll
from textual.widgets import Button, ContentSwitcher, DataTable, Input, ListView, Select, Static
from typer.testing import CliRunner

from daino import branding
from daino.application import (
    MissionApplicationService,
    ProviderApplicationService,
    initialize_project,
    open_project,
)
from daino.application.view_models import OpenRouterModel, ProviderStatus
from daino.cli.app import app
from daino.config import config_path
from daino.events import (
    ApprovalRequested,
    DeploymentProgress,
    FileChanged,
    MissionCompleted,
    ModelReasoningChunk,
    ModelSelected,
    ModelStreamChunk,
    ToolCompleted,
    ToolFailed,
    ToolStarted,
)
from daino.events import (
    TestsCompleted as VerificationCompletedEvent,
)
from daino.events import (
    TestsStarted as VerificationStartedEvent,
)
from daino.persistence.models import ModelCall, ToolCall
from daino.schemas import InteractionMode, QAReport, TodoItem
from daino.tui.app import DainoApp
from daino.tui.keybindings import SLASH_COMMANDS
from daino.tui.screens.onboarding import OnboardingScreen
from daino.tui.screens.views import ProvidersView, QAView
from daino.tui.screens.workspace import WorkspaceScreen
from daino.tui.widgets import (
    ApprovalModal,
    CommandPalette,
    ConversationView,
    DainoHintBar,
    NavigationTab,
    NavigationTabs,
    PromptInput,
    TaskChecklist,
)
from daino.tui.widgets.message import MessageCard
from daino.tui.widgets.prompt_input import PromptTextArea
from tests.conftest import commit_all, painted_text


def initialized_app(root: Path) -> DainoApp:
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    commit_all(root)
    initialize_project(root)
    return DainoApp(root, context=open_project(root))


def test_tui_uses_launch_directory_even_beneath_another_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "parent"
    (parent / ".git").mkdir(parents=True)
    (parent / ".vasuki").mkdir()
    child = parent / "test1"
    child.mkdir()
    monkeypatch.chdir(child)

    app_instance = DainoApp()

    assert app_instance.project == child.resolve()
    assert config_path(app_instance.project) == child.resolve() / ".daino" / "config.yaml"


@pytest.mark.asyncio
async def test_tui_launches_existing_project_and_navigation_works(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, WorkspaceScreen)
        assert app_instance.screen.query_one(PromptInput)
        switcher = app_instance.screen.query_one("#main-workspace", ContentSwitcher)
        assert switcher.current == "chat-view"

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert switcher.current == "files-view"

        await app_instance.screen.execute_command("/missions")
        assert switcher.current == "missions-view"

        await app_instance.screen.execute_command("/qa")
        await pilot.pause()
        assert switcher.current == "qa-view"
        assert app_instance.screen.query_one("#qa-view", QAView)
        tabs = app_instance.screen.query_one("#nav-tabs", NavigationTabs)
        assert [tab.view_id for tab in tabs.query(NavigationTab)][:3] == [
            "chat-view",
            "missions-view",
            "qa-view",
        ]
        scroll = app_instance.screen.query_one("#qa-scroll", VerticalScroll)
        assert scroll.max_scroll_y > 0


@pytest.mark.asyncio
async def test_completing_a_task_shows_a_readable_line_in_the_conversation(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        # A task transitioning to completed must surface a readable, ticked line,
        # not only bump the side-panel counter.
        workspace.missions.set_session_todos(
            workspace.session_id,
            [
                TodoItem(content="Find book cover URLs", status="completed"),
                TodoItem(content="Rewrite index.html", status="in_progress"),
            ],
        )
        await pilot.pause()
        await pilot.pause()

        painted = painted_text(app_instance)
        assert "Find book cover URLs" in painted
        assert "✓" in painted


@pytest.mark.asyncio
async def test_successful_turn_clears_a_previous_failure_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daino.schemas import ChatOutcome

    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        # A prior turn left the header/mission strip showing a failure.
        workspace.active_status = "Failed"
        workspace._chat_previous_status = None
        workspace.query_one("#context-strip").set_mission("mission-old", "Failed")

        async def fake_chat(instruction, session_id, *, profile_override="", approve=None):
            return ChatOutcome(
                mission_id="mission-new",
                summary="done",
                changed=["index.html"],
                steps=3,
                verified=True,
            )

        monkeypatch.setattr(workspace.missions, "chat", fake_chat)
        workspace.run_chat_agent("redesign the page")
        await app_instance.workers.wait_for_complete()
        await pilot.pause()

        # A successful turn returns the header to Ready (never lingering "Failed"),
        # and the mission strip reflects the new completed mission, not the old one.
        assert workspace.active_status == "Ready"
        painted = painted_text(app_instance)
        assert "mission-new completed" in painted
        assert "mission-old failed" not in painted


@pytest.mark.asyncio
async def test_workspace_chrome_is_compact_and_quiet(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        header = workspace.query_one("#top-header")
        tabs = workspace.query_one("#nav-tabs", NavigationTabs)
        context = workspace.query_one("#context-strip")
        painted = painted_text(app_instance)

        assert header.region.height == 4
        assert tabs.region.height == 2
        assert context.region.height == 2
        assert branding.NAME in painted
        assert "not configured" in painted
        assert "chat" in painted
        assert "missions" in painted
        assert "ctrl+p commands" in painted

        active = workspace.query_one("#tab-chat-view", NavigationTab).render()
        assert active.plain.startswith(" chat ")
        assert workspace.query_one("#tab-chat-view", NavigationTab).active


@pytest.mark.asyncio
async def test_clicking_each_primary_tab_switches_the_workspace(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        switcher = workspace.query_one("#main-workspace", ContentSwitcher)

        for view_id in (
            "missions-view",
            "qa-view",
            "files-view",
            "changes-view",
            "tests-view",
            "logs-view",
            "map-view",
            "chat-view",
        ):
            assert await pilot.click(f"#tab-{view_id}")
            await pilot.pause()
            assert switcher.current == view_id
            assert [tab.view_id for tab in workspace.query(NavigationTab) if tab.active] == [
                view_id
            ]


@pytest.mark.asyncio
async def test_prompt_map_lists_runs_and_draws_safe_token_graph(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit_all(tmp_path)
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    service = MissionApplicationService(context)
    older = service.core.create("First prompt")
    newer = service.core.create("Second prompt")
    with context.database.session() as session:
        session.add_all(
            [
                ModelCall(
                    id="model-map-old",
                    mission_id=older.id,
                    role="builder",
                    provider="local-ollama",
                    model="qwen3.8",
                    selection_reason="selected for this session",
                    input_tokens=120,
                    output_tokens=30,
                    latency_ms=250,
                    estimated_cost=0.0,
                    success=True,
                ),
                ToolCall(
                    id="tool-map-old",
                    mission_id=older.id,
                    tool="chat.read_file",
                    arguments={
                        "thought": "PRIVATE REASONING MUST STAY HIDDEN",
                        "action": "read_file",
                        "path": "app.py",
                        "content": "PRIVATE FILE CONTENT",
                    },
                    result_summary="ok",
                    duration_seconds=0.02,
                    success=True,
                ),
                ModelCall(
                    id="model-map-new",
                    mission_id=newer.id,
                    role="summarizer",
                    provider="local-ollama",
                    model="qwen3.8",
                    selection_reason="selected for this session",
                    input_tokens=40,
                    output_tokens=10,
                    latency_ms=100,
                    estimated_cost=0.0,
                    success=True,
                ),
            ]
        )
    app_instance = DainoApp(tmp_path, context=context)

    async with app_instance.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        current_session = workspace.session_id

        await workspace.execute_command("/map")
        await pilot.pause()

        table = workspace.query_one("#map-prompts", DataTable)
        assert table.row_count == 2
        assert "Second prompt" in workspace.query_one("#map-trace-summary", Static).render().plain
        assert "50 tokens" in workspace.query_one("#map-trace-summary", Static).render().plain

        table.focus()
        table.move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause()

        graph = workspace.query_one("#map-graph", Static).render().plain
        assert "First prompt" in workspace.query_one("#map-trace-summary", Static).render().plain
        assert "MODEL" in graph
        assert "120 in + 30 out = 150" in graph
        assert "TOOL" in graph
        assert "app.py" in graph
        assert "PRIVATE REASONING" not in graph
        assert "PRIVATE FILE CONTENT" not in graph
        assert workspace.session_id == current_session


@pytest.mark.asyncio
async def test_qa_run_button_starts_the_workspace_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        started: list[bool] = []
        monkeypatch.setattr(workspace, "run_qa", lambda: started.append(True))

        await workspace.execute_command("/qa")
        await pilot.pause()
        await pilot.click("#run-qa")
        await pilot.pause()

        assert started == [True]


@pytest.mark.asyncio
async def test_qa_tab_lists_and_loads_repository_scan_history(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        report = QAReport(
            id="qa-saved",
            status="completed",
            started_at=datetime.now(UTC),
            project_root=str(tmp_path.resolve()),
            project_profile=["python"],
            summary="# Saved report\n\nPreviously discovered evidence.",
        )
        workspace.qa._save(report)

        workspace.action_open_view("qa-view")
        await pilot.pause()
        history = workspace.query_one("#qa-history", DataTable)
        assert history.row_count == 1

        history.focus()
        history.move_cursor(row=0)
        history.action_select_cursor()
        await pilot.pause()

        assert workspace._last_qa_report == report
        state = str(workspace.query_one("#qa-state").render())
        assert "qa-saved" in state
        assert "Completed" in state


@pytest.mark.asyncio
async def test_shift_tab_cycles_persists_and_highlights_the_session_mode(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        assert workspace.interaction_mode == InteractionMode.ASK

        await pilot.press("shift+tab")
        await pilot.pause()

        assert workspace.interaction_mode == InteractionMode.SESSION
        assert workspace.missions.interaction_mode(workspace.session_id) == InteractionMode.SESSION
        hint = workspace.query_one("#hint-bar", DainoHintBar).render()
        assert "SESSION" in hint.plain
        mode_span = next(
            span for span in hint.spans if "SESSION" in hint.plain[span.start : span.end]
        )
        assert "bold" in str(mode_span.style)
        assert " on " not in str(mode_span.style)


@pytest.mark.asyncio
async def test_plan_mode_routes_bare_requests_to_planning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        planned: list[str] = []
        chatted: list[str] = []
        monkeypatch.setattr(workspace, "plan_mission", planned.append)
        monkeypatch.setattr(workspace, "run_chat_agent", chatted.append)
        workspace._set_interaction_mode(InteractionMode.PLAN)

        await workspace.execute_command("add a health endpoint")

        assert planned == ["add a health endpoint"]
        assert chatted == []


@pytest.mark.asyncio
async def test_full_mode_resolves_mission_gates_without_a_modal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        approvals: list[tuple[str, dict[str, object]]] = []
        executions: list[str] = []
        monkeypatch.setattr(
            workspace.missions,
            "approve",
            lambda mission_id, **kwargs: approvals.append((mission_id, kwargs)),
        )
        monkeypatch.setattr(workspace, "execute_mission", executions.append)
        workspace._set_interaction_mode(InteractionMode.FULL)

        workspace._show_approval(
            ApprovalRequested(
                mission_id="M-full",
                category="mission_execution",
                subject="Execute the plan",
            )
        )
        await pilot.pause(0.1)

        assert isinstance(app_instance.screen, WorkspaceScreen)
        assert approvals == [
            (
                "M-full",
                {
                    "approved": True,
                    "scope": "full",
                    "category": "mission_execution",
                },
            )
        ]
        assert executions == ["M-full"]


@pytest.mark.asyncio
async def test_onboarding_appears_when_project_is_uninitialized(tmp_path: Path) -> None:
    app_instance = DainoApp(tmp_path)
    async with app_instance.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, OnboardingScreen)
        assert app_instance.screen.query_one("#initialize")


@pytest.mark.asyncio
async def test_choosing_ollama_offers_the_models_it_has_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare text field made the user recall an exact tag like "qwen3.8:27b-mlx"."""
    from daino.application.view_models import CatalogModel
    from daino.tui.screens import onboarding as onboarding_module

    async def installed(base_url: str = "") -> list[CatalogModel]:
        return [CatalogModel(id="qwen3.8:27b-mlx", name="qwen3.8:27b-mlx", detail="16.9 GB")]

    monkeypatch.setattr(onboarding_module, "list_ollama_models", installed)
    app_instance = DainoApp(tmp_path)
    async with app_instance.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        screen = app_instance.screen
        assert isinstance(screen, OnboardingScreen)
        screen.query_one("#provider-choice", Select).value = "ollama"
        for _ in range(40):
            await pilot.pause(0.05)
            if screen.query_one("#provider-model-select", Select).value is not Select.BLANK:
                break

        selector = screen.query_one("#provider-model-select", Select)
        assert not selector.has_class("hidden"), "the model picker never appeared"
        assert screen.query_one("#provider-model", Input).has_class("hidden")
        assert selector.value == "qwen3.8:27b-mlx"


@pytest.mark.asyncio
async def test_an_unreachable_ollama_leaves_the_model_field_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama may not be running yet; onboarding must stay completable."""
    from daino.tui.screens import onboarding as onboarding_module

    async def unreachable(base_url: str = "") -> list[object]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(onboarding_module, "list_ollama_models", unreachable)
    app_instance = DainoApp(tmp_path)
    async with app_instance.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        screen = app_instance.screen
        assert isinstance(screen, OnboardingScreen)
        screen.query_one("#provider-choice", Select).value = "ollama"
        for _ in range(40):
            await pilot.pause(0.05)
            if not screen.query_one("#provider-model", Input).has_class("hidden"):
                break

        assert not screen.query_one("#provider-model", Input).has_class("hidden")
        assert screen.query_one("#provider-model-select", Select).has_class("hidden")


@pytest.mark.asyncio
async def test_command_palette_and_narrow_layout(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(68, 28)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        assert workspace.has_class("very-narrow")
        # The single-column layout keeps chat and prompt at full width.
        assert workspace.has_class("narrow")
        assert workspace.query_one("#welcome-help").display
        assert workspace.query_one("#chat-view").region.width == 68
        assert not workspace.query_one("#context-strip").has_class("hidden-panel")

        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app_instance.screen, CommandPalette)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_enter_submits_plain_prompt_and_shift_enter_adds_newline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        requests: list[str] = []
        monkeypatch.setattr(workspace, "run_chat_agent", requests.append)
        prompt = workspace.query_one(PromptInput).query_one("#prompt")

        await pilot.press("f", "i", "x", "space", "i", "t", "enter")
        await pilot.pause()
        assert requests == ["fix it"]
        assert prompt.text == ""
        assert any(card.raw_content == "fix it" for card in workspace.query(MessageCard))

        await pilot.press("l", "i", "n", "e", "shift+enter", "t", "w", "o")
        await pilot.pause()
        assert prompt.text == "line\ntwo"


@pytest.mark.asyncio
async def test_prompt_is_a_compact_multiline_paste_surface(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        composer = workspace.query_one(PromptInput)
        prompt = composer.query_one("#prompt", PromptTextArea)
        caret = composer.query_one("#prompt-caret")
        hint = workspace.query_one("#hint-bar", DainoHintBar)

        assert composer.region.height >= 4
        assert prompt.region.height >= 2
        assert composer.region.y + composer.region.height == hint.region.y
        assert caret.content_region.y == prompt.content_region.y
        assert prompt.content_region.x == caret.region.x + caret.region.width

        prompt.focus()
        await prompt._on_paste(  # noqa: SLF001 - exercise Textual's bracketed-paste path
            textual_events.Paste("first line\r\nsecond line\rthird line")
        )
        await pilot.pause()

        assert prompt.text == "first line\nsecond line\nthird line"
        rendered = painted_text(app_instance)
        assert "PASTED · 3 lines" in rendered


@pytest.mark.asyncio
async def test_prompt_completion_preserves_pasted_newlines(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        composer = workspace.query_one(PromptInput)
        prompt = composer.query_one("#prompt", PromptTextArea)
        prompt.load_text("Keep this pasted line intact.\n@fi")

        composer._apply_suggestion("@file:app.py")  # noqa: SLF001 - completion regression

        assert prompt.text == "Keep this pasted line intact.\n@file:app.py"


@pytest.mark.asyncio
async def test_task_sidebar_tracks_current_agent_activity(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("/verbose on")
        checklist = workspace.query_one("#task-checklist", TaskChecklist)

        workspace.context.events.publish(
            ToolStarted(mission_id="mission-ui", tool="read_file", summary="Inspect app.py")
        )
        await pilot.pause()
        assert checklist.display
        assert checklist.activity_state == "inspecting"
        assert "INSPECTING" in str(checklist.query_one("#activity-label").render())
        runner = checklist.query_one("#runner-stage")
        assert runner.region.height == 5
        first_frame = str(runner.render())
        checklist._advance_runner()  # noqa: SLF001 - deterministic animation frame
        assert str(runner.render()) != first_frame
        assert checklist._animation_running  # noqa: SLF001 - timer follows real work state

        workspace.context.events.publish(
            ToolStarted(mission_id="mission-ui", tool="replace", summary="Update app.py")
        )
        await pilot.pause()
        assert checklist.activity_state == "building"

        workspace.context.events.publish(
            ToolFailed(mission_id="mission-ui", tool="replace", error="Patch did not apply")
        )
        await pilot.pause()
        crashed_frame = str(runner.render())
        assert checklist.activity_state == "failed"
        assert "×" in crashed_frame
        assert "ERROR" in str(checklist.query_one("#activity-label").render())
        assert "Patch did not apply" in painted_text(app_instance)
        assert not checklist._animation_running  # noqa: SLF001
        checklist._advance_runner()  # noqa: SLF001 - crash frame remains frozen
        assert str(runner.render()) == crashed_frame

        workspace.context.events.publish(
            VerificationStartedEvent(mission_id="mission-ui", commands=["pytest -q"])
        )
        await pilot.pause()
        assert checklist.activity_state == "verifying"

        workspace.context.events.publish(
            MissionCompleted(mission_id="mission-ui", evidence_path="evidence.json")
        )
        await pilot.pause()
        assert checklist.activity_state == "completed"
        assert "TASK COMPLETED" in painted_text(app_instance)
        assert not checklist._animation_running  # noqa: SLF001


@pytest.mark.asyncio
async def test_verbose_command_controls_live_operational_detail(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        checklist = workspace.query_one("#task-checklist", TaskChecklist)

        await workspace.execute_command("/verbose off")

        workspace.context.events.publish(
            ToolStarted(mission_id="quiet", tool="read_file", summary="Inspect secret.py")
        )
        workspace.context.events.publish(
            ToolCompleted(
                mission_id="quiet",
                tool="read_file",
                summary="Inspected secret.py",
                duration_seconds=0.1,
            )
        )
        await pilot.pause()

        assert checklist.activity_state == "working"
        assert "Inspect secret.py" not in painted_text(app_instance)

        await workspace.execute_command("/verbose on")
        conversation = workspace.query_one("#chat-view", ConversationView)
        await conversation.begin_pending("understanding request")
        workspace.context.events.publish(
            ModelSelected(
                mission_id="loud",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        await pilot.pause()
        assert "local-ollama generating the next action" in painted_text(app_instance)
        workspace.context.events.publish(
            ToolStarted(mission_id="loud", tool="read_file", summary="Inspect visible.py")
        )
        workspace.context.events.publish(
            ToolCompleted(
                mission_id="loud",
                tool="read_file",
                summary="Inspected visible.py",
                duration_seconds=0.1,
            )
        )
        await pilot.pause()

        assert checklist.activity_state == "inspecting"
        assert "Inspected visible.py" in painted_text(app_instance)


@pytest.mark.asyncio
async def test_verbose_reasoning_is_ephemeral_and_resets_at_call_boundaries(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("/verbose on")
        conversation = workspace.query_one("#chat-view", ConversationView)
        await conversation.begin_pending("generating the next action")
        bus = workspace.context.events

        bus.publish(
            ModelSelected(
                mission_id="reasoning-run",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        bus.publish(
            ModelReasoningChunk(
                mission_id="reasoning-run",
                content="Inspecting the requested page structure.",
                role="builder",
            )
        )
        await pilot.pause(0.12)

        assert conversation.query_one("#live-reasoning", Static)
        assert "Inspecting the requested page structure." in conversation.reasoning_text
        assert "thinking · live recent" in painted_text(app_instance)
        assert all(
            "Inspecting the requested page structure." not in card.raw_content
            for card in conversation.query(MessageCard)
        )
        assert all(
            "Inspecting the requested page structure." not in item.content
            for item in workspace.missions.messages(workspace.session_id)
        )

        # A new selection starts a different model call and must discard the old
        # live tail before its first chunk arrives.
        bus.publish(
            ModelSelected(
                mission_id="reasoning-run",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        await pilot.pause()
        assert conversation.reasoning_text == ""
        assert not list(conversation.query("#live-reasoning"))

        bus.publish(
            ModelReasoningChunk(
                mission_id="reasoning-run",
                content="Choosing a safe write operation.",
                role="builder",
            )
        )
        await pilot.pause(0.12)
        bus.publish(
            ToolStarted(
                mission_id="reasoning-run",
                tool="chat.write",
                summary="Write index.html",
            )
        )
        await pilot.pause()
        assert conversation.reasoning_text == ""
        assert not list(conversation.query("#live-reasoning"))

        # User-facing stream chunks get their own answer card; reasoning never
        # becomes its prefix or metadata.
        bus.publish(
            ModelSelected(
                mission_id="reasoning-run",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        bus.publish(
            ModelReasoningChunk(
                mission_id="reasoning-run",
                content="private working tail",
                role="builder",
            )
        )
        bus.publish(
            ModelStreamChunk(
                mission_id="reasoning-run",
                content="Visible answer",
                role="builder",
            )
        )
        await pilot.pause(0.12)

        assert conversation.reasoning_text == ""
        agent_cards = [card for card in conversation.query(MessageCard) if card.kind == "agent"]
        assert any(card.raw_content == "Visible answer" for card in agent_cards)
        assert all("private working tail" not in card.raw_content for card in agent_cards)

        bus.publish(
            ModelSelected(
                mission_id="reasoning-run",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        bus.publish(
            ModelReasoningChunk(
                mission_id="reasoning-run",
                content="final private tail",
                role="builder",
            )
        )
        await pilot.pause(0.12)
        assert conversation.reasoning_text
        bus.publish(MissionCompleted(mission_id="reasoning-run", evidence_path="evidence.json"))
        await pilot.pause()
        assert conversation.reasoning_text == ""
        assert not list(conversation.query("#live-reasoning"))


@pytest.mark.asyncio
async def test_reasoning_is_ignored_when_verbose_is_off_and_logs_stay_safe(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("/verbose off")
        conversation = workspace.query_one("#chat-view", ConversationView)
        await conversation.begin_pending("working")
        raw_reasoning = "never show this private reasoning"

        workspace.context.events.publish(
            ModelSelected(
                mission_id="quiet-reasoning",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        for _ in range(20):
            workspace.context.events.publish(
                ModelReasoningChunk(
                    mission_id="quiet-reasoning",
                    content=raw_reasoning,
                    role="builder",
                )
            )
        await pilot.pause(0.12)

        assert conversation.reasoning_text == ""
        assert not list(conversation.query("#live-reasoning"))
        assert raw_reasoning not in painted_text(app_instance)
        logs = workspace.query_one("#logs-view")
        assert logs._live_label == "Working…"  # noqa: SLF001 - coalesced safe state
        assert len(logs.query_one("#log-live-content").lines) == 0
        audit_path = tmp_path / ".daino" / "logs" / "events.jsonl"
        if audit_path.exists():
            assert raw_reasoning not in audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reasoning_is_markup_safe_redacted_and_bounded_across_split_chunks(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("/verbose on")
        conversation = workspace.query_one("#chat-view", ConversationView)
        await conversation.begin_pending("working")
        bus = workspace.context.events
        bus.publish(
            ModelSelected(
                mission_id="safe-reasoning",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen",
                role="builder",
            )
        )
        # Both credential forms are deliberately split across event boundaries.
        bus.publish(
            ModelReasoningChunk(
                mission_id="safe-reasoning",
                content="[bold red]literal markup[/]\x1b[31m\x00 api_",
                role="builder",
            )
        )
        bus.publish(
            ModelReasoningChunk(
                mission_id="safe-reasoning",
                content="key=supersecret sk-",
                role="builder",
            )
        )
        bus.publish(
            ModelReasoningChunk(
                mission_id="safe-reasoning",
                content="A" * 24,
                role="builder",
            )
        )
        await pilot.pause(0.12)

        safe_tail = conversation.reasoning_text
        assert "[bold red]literal markup[/]" in safe_tail
        assert "[bold red]literal markup[/]" in painted_text(app_instance)
        assert "\x1b" not in safe_tail
        assert "\x00" not in safe_tail
        assert "supersecret" not in safe_tail
        assert "sk-" not in safe_tail
        assert safe_tail.count("[REDACTED]") == 2

        oversized = "\n".join(f"line-{index}-" + "x" * 240 for index in range(30))
        bus.publish(
            ModelReasoningChunk(
                mission_id="safe-reasoning",
                content=oversized,
                role="builder",
            )
        )
        await pilot.pause(0.12)

        assert len(conversation.reasoning_text) <= conversation.REASONING_MAX_CHARS
        assert len(conversation.reasoning_text.splitlines()) <= conversation.REASONING_MAX_LINES


@pytest.mark.asyncio
async def test_logs_live_section_tracks_current_model_and_tool(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        workspace.context.events.publish(
            ModelSelected(
                mission_id="mission-live",
                profile="local-ollama",
                provider="local-ollama",
                model="qwen3.8",
                role="builder",
            )
        )
        workspace.context.events.publish(
            ToolStarted(
                mission_id="mission-live",
                tool="chat.read_file",
                summary="Inspect app.py",
            )
        )
        await pilot.pause()
        await workspace.execute_command("/logs")
        await pilot.pause()

        painted = painted_text(app_instance)
        assert "Live activity" in painted
        assert "Builder using qwen3.8" in painted
        assert "chat.read_file: Inspect app.py" in painted


@pytest.mark.asyncio
async def test_returning_from_providers_focuses_chat_and_enter_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        requests: list[str] = []
        monkeypatch.setattr(workspace, "run_chat_agent", requests.append)
        switcher = workspace.query_one("#main-workspace", ContentSwitcher)

        workspace.action_open_view("providers-view")
        await pilot.pause()
        assert switcher.current == "providers-view"
        await pilot.click("#tab-chat-view")
        await pilot.pause()
        assert switcher.current == "chat-view"
        await pilot.press("h", "i", "enter")
        await pilot.pause()

        assert requests == ["hi"]
        assert any(card.raw_content == "hi" for card in workspace.query(MessageCard))


@pytest.mark.asyncio
async def test_globalprovider_command_and_project_global_button(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        view = workspace.query_one("#providers-view", ProvidersView)

        await workspace.execute_command("/globalprovider")
        assert view.scope == "global"
        assert not view.query_one("#use-global-provider", Button).display

        await workspace.execute_command("/provider")
        assert view.scope == "project"
        assert view.query_one("#use-global-provider", Button).display


@pytest.mark.asyncio
async def test_slash_opens_navigable_command_dropdown(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        prompt_input = workspace.query_one(PromptInput)
        suggestions = prompt_input.query_one("#prompt-suggestions", ListView)

        await pilot.press("/")
        await pilot.pause()
        assert not suggestions.has_class("hidden")
        assert len(suggestions.children) == len(SLASH_COMMANDS)
        assert suggestions.index == 0
        assert prompt_input.region.contains_region(suggestions.region)
        rendered = painted_text(app_instance)
        assert "/help" in rendered
        assert "Open help" in rendered

        await pilot.press("down", "enter")
        await pilot.pause()
        assert prompt_input.text == "/clear "
        assert suggestions.has_class("hidden")


@pytest.mark.asyncio
async def test_command_drawer_grows_to_matches_without_moving_the_input(tmp_path: Path) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        composer = workspace.query_one(PromptInput)
        prompt = composer.query_one("#prompt", PromptTextArea)
        suggestions = composer.query_one("#prompt-suggestions", ListView)
        baseline_y = prompt.region.y
        collapsed_height = composer.region.height

        await pilot.press("/", "m", "o")
        await pilot.pause()

        assert len(suggestions.children) == 2
        assert prompt.region.y == baseline_y
        assert composer.region.height == collapsed_height + 2


@pytest.mark.asyncio
async def test_bye_submitted_with_enter_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        exits: list[bool] = []
        monkeypatch.setattr(app_instance, "exit", lambda *args, **kwargs: exits.append(True))

        await pilot.press("/", "b", "y", "e", "enter")
        await pilot.pause()
        assert exits == [True]


@pytest.mark.asyncio
async def test_openrouter_selection_shows_provider_and_model_dropdowns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace.action_open_view("providers-view")
        view = workspace.query_one("#providers-view", ProvidersView)

        async def models(**_: str) -> list[OpenRouterModel]:
            return [
                OpenRouterModel(
                    id="anthropic/test-model",
                    name="Anthropic Test Model",
                    context_length=200_000,
                ),
                OpenRouterModel(
                    id="openai/test-model",
                    name="OpenAI Test Model",
                    context_length=128_000,
                ),
            ]

        monkeypatch.setattr(view.service, "openrouter_models", models)
        provider_type = view.query_one("#provider-type", Select)
        provider_type.value = "openrouter"
        for _ in range(10):
            await pilot.pause(0.05)
            if "Loaded 2" in str(view.query_one("#provider-form-status").render()):
                break

        model_input = view.query_one("#provider-model", Input)
        model_search = view.query_one("#provider-model-search", Input)
        model_select = view.query_one("#provider-model-select", Select)
        assert provider_type.value == "openrouter"
        assert view.query_one("#provider-name", Input).value == "openrouter"
        assert view.query_one("#provider-base-url", Input).value == "https://openrouter.ai/api/v1"
        assert model_input.has_class("hidden")
        assert not model_search.has_class("hidden")
        assert not model_select.has_class("hidden")
        assert [value for _, value in model_select._options if value is not Select.BLANK] == [
            "anthropic/test-model",
            "openai/test-model",
        ]

        model_search.value = "openai test"
        await pilot.pause()
        assert [value for _, value in model_select._options if value is not Select.BLANK] == [
            "openai/test-model"
        ]
        assert "Showing 1 of 2" in str(view.query_one("#provider-form-status").render())
        model_search.value = ""
        await pilot.pause()

        model_select.focus()
        model_select.action_show_overlay()
        await pilot.pause()
        rendered = painted_text(app_instance)
        assert "Provider type" in rendered
        assert "OpenRouter" in rendered
        assert "Anthropic Test Model" in rendered
        assert "OpenAI Test Model" in rendered


@pytest.mark.asyncio
async def test_saved_openrouter_provider_and_model_are_restored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    initialize_project(tmp_path)
    context = open_project(tmp_path)
    ProviderApplicationService(context).add(
        name="openrouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="openai/test-model",
        api_key_reference=f"file://{tmp_path}/unused.key",
    )

    async def models(*_: object, **__: str) -> list[OpenRouterModel]:
        return [
            OpenRouterModel(
                id="openai/test-model",
                name="OpenAI Test Model",
                context_length=128_000,
            )
        ]

    monkeypatch.setattr(
        ProviderApplicationService,
        "openrouter_models",
        models,
    )
    monkeypatch.setattr(WorkspaceScreen, "provider_health", lambda self: None)
    app_instance = DainoApp(tmp_path, context=context)
    async with app_instance.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace.action_open_view("providers-view")
        for _ in range(10):
            await pilot.pause(0.05)
            view = workspace.query_one("#providers-view", ProvidersView)
            if view.query_one("#provider-model-select", Select).value is not Select.BLANK:
                break

        assert view.query_one("#provider-type", Select).value == "openrouter"
        assert view.query_one("#provider-name", Input).value == "openrouter"
        assert view.query_one("#provider-base-url", Input).value == "https://openrouter.ai/api/v1"
        assert view.query_one("#provider-model-select", Select).value == "openai/test-model"
        assert not view.query_one("#provider-model-search", Input).has_class("hidden")


@pytest.mark.asyncio
async def test_connecting_provider_replaces_previous_session_model_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace.providers.add(
            name="openrouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="openai/test-model",
            api_key_reference="env://OPENROUTER_API_KEY",
        )
        workspace.providers.select_for_session(workspace.session_id, "openrouter")
        workspace.providers.add(
            name="local-ollama",
            provider_type="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3.8:27b-mlx",
        )
        assert workspace.providers.session_profile(workspace.session_id) == "openrouter"

        async def configured(**_: object) -> tuple[ProviderStatus, list[OpenRouterModel]]:
            return (
                ProviderStatus(
                    name="local-ollama",
                    type="ollama",
                    base_url="http://127.0.0.1:11434/v1",
                    model="qwen3.8:27b-mlx",
                    connected=True,
                    detail="17 ms; routed all agent roles",
                ),
                [],
            )

        monkeypatch.setattr(workspace.providers, "configure", configured)
        workspace.save_provider(
            {
                "name": "local-ollama",
                "provider_type": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen3.8:27b-mlx",
                "api_key_input": "",
            }
        )
        for _ in range(20):
            await pilot.pause(0.05)
            if workspace.providers.session_profile(workspace.session_id) == "local-ollama":
                break

        assert workspace.providers.session_profile(workspace.session_id) == "local-ollama"
        assert workspace.active_model == "local-ollama"


@pytest.mark.asyncio
async def test_chat_scroll_follows_bottom_but_preserves_manual_position(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        conversation = workspace.query_one("#chat-view", ConversationView)

        for index in range(24):
            await conversation.add_message(
                f"History item {index}\n"
                "A sufficiently long line to create a scrollable conversation.",
                kind="agent",
                follow=True,
            )
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y

        conversation.scroll_to(y=0, animate=False, force=True, immediate=True)
        await pilot.pause()
        await conversation.add_message(
            "Background response while reading older messages.",
            kind="agent",
        )
        await pilot.pause()
        assert conversation.scroll_y == 0
        assert conversation.max_scroll_y > 0

        await conversation.add_message("My new prompt", kind="user", follow=True)
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y

        workspace.query_one(PromptInput).focus_prompt()
        await pilot.press("pageup")
        await pilot.pause()
        assert conversation.scroll_y < conversation.max_scroll_y


@pytest.mark.asyncio
async def test_invalid_openrouter_key_reason_is_shown_without_saving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        workspace.action_open_view("providers-view")
        view = workspace.query_one("#providers-view", ProvidersView)

        async def no_models(**_: str) -> list[OpenRouterModel]:
            return []

        async def invalid(**_: str) -> object:
            raise ValueError(
                "Provider was not saved: OpenRouter API key rejected (HTTP 401): User not found."
            )

        monkeypatch.setattr(view.service, "openrouter_models", no_models)
        monkeypatch.setattr(view.service, "configure", invalid)
        view.query_one("#provider-type", Select).value = "openrouter"
        await pilot.pause()
        model_select = view.query_one("#provider-model-select", Select)
        model_select.set_options([("Test Model", "openai/test-model")])
        model_select.value = "openai/test-model"
        view.query_one("#provider-secret", Input).value = "invalid-key"

        await pilot.click("#save-provider")
        for _ in range(10):
            await pilot.pause(0.05)
            status = str(view.query_one("#provider-form-status").render())
            if "HTTP 401" in status:
                break

        assert "HTTP 401" in status
        assert "User not found" in status
        assert "openrouter" not in workspace.context.settings.providers


@pytest.mark.asyncio
async def test_plan_command_creates_plan_and_rejection_does_not_reopen_approval(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)

        prompt = workspace.query_one(PromptInput).query_one("#prompt")
        prompt.load_text("/plan Add a health endpoint and tests.")
        prompt.focus()
        await pilot.press("enter")
        for _ in range(15):
            await pilot.pause(0.1)
            if isinstance(app_instance.screen, ApprovalModal):
                break
        assert isinstance(app_instance.screen, ApprovalModal)
        await pilot.pause()
        checklist = workspace.query_one("#task-checklist", TaskChecklist)
        assert checklist.display
        assert checklist.todos

        await pilot.click("#reject")
        await pilot.pause()
        details = workspace.missions.mission_details(workspace.active_mission_id or "")
        assert details["mission"]["status"] == "blocked"
        assert len(details["approvals"]) == 1
        assert workspace.missions.messages(workspace.session_id)[-1].kind == "plan"

        workspace.context.events.publish(
            ApprovalRequested(
                mission_id=workspace.active_mission_id,
                category="mission_execution",
                subject="Approve the implementation plan",
            )
        )
        await pilot.pause()
        assert isinstance(app_instance.screen, WorkspaceScreen)


@pytest.mark.asyncio
async def test_live_events_render_without_blocking_and_refresh_context(
    tmp_path: Path,
) -> None:
    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        await workspace.execute_command("/verbose on")
        bus = workspace.context.events
        bus.publish(ModelStreamChunk(mission_id="M-live", content="Streaming ", role="builder"))
        bus.publish(ModelStreamChunk(mission_id="M-live", content="answer", role="builder"))
        bus.publish(
            ToolCompleted(
                mission_id="M-live",
                tool="edit.patch",
                summary="Updated app.py",
                duration_seconds=0.1,
            )
        )
        bus.publish(FileChanged(mission_id="M-live", path="app.py", action="patch"))
        bus.publish(
            VerificationCompletedEvent(
                mission_id="M-live",
                passed=False,
                failed_count=1,
                failures=[{"summary": "expected 200"}],
            )
        )
        bus.publish(DeploymentProgress(target="development", stage="Health checks", progress=0.8))
        await pilot.pause()

        cards = workspace.query(MessageCard)
        contents = [card.raw_content for card in cards]
        assert any("Streaming answer" in content for content in contents)
        assert any("Updated app.py" in content for content in contents)
        assert any("1 failed" in content for content in contents)
        assert "Health checks" in str(workspace.query_one("#context-strip").render())


@pytest.mark.asyncio
async def test_each_launch_starts_a_fresh_session(tmp_path: Path) -> None:
    """Reopening a project must not resume the previous conversation.

    Resuming reloaded the whole transcript and fed it back as conversation
    history on the next prompt, so every new question paid for context from a
    conversation that was already finished.
    """
    first_app = initialized_app(tmp_path)
    async with first_app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        first = first_app.screen
        assert isinstance(first, WorkspaceScreen)
        first.missions.add_message(
            first.session_id,
            kind="agent",
            role="architect",
            content="Persisted response",
        )
        session_id = first.session_id

    second_context = open_project(tmp_path)
    second_app = DainoApp(tmp_path, context=second_context)
    async with second_app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        second = second_app.screen
        assert isinstance(second, WorkspaceScreen)

        assert second.session_id != session_id
        # The new session starts empty: nothing from last time is re-sent.
        assert second.missions.messages(second.session_id) == []
        assert "Persisted response" not in [card.raw_content for card in second.query(MessageCard)]
        # The earlier conversation is not lost, only left behind.
        assert "Persisted response" in [
            item.content for item in second.missions.messages(session_id)
        ]


def test_bare_cli_and_explicit_tui_launch_use_tui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path | None] = []
    monkeypatch.setattr("daino.tui.run_tui", calls.append)
    runner = CliRunner()

    result = runner.invoke(app, ["--project", str(tmp_path)])
    explicit = runner.invoke(app, ["tui", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert explicit.exit_code == 0
    assert calls == [tmp_path.resolve(), tmp_path]


@pytest.mark.asyncio
async def test_a_new_directory_offers_global_or_project_specific_settings(
    tmp_path: Path,
) -> None:
    """New projects make configuration inheritance an explicit choice."""
    from daino.config.globals import save_global
    from daino.config.models import ModelProfileConfig, ProviderConfig, Settings

    configured = Settings(project={"name": "any"})
    configured.providers = {
        "openrouter": ProviderConfig(
            type="openrouter", base_url="https://openrouter.ai/api/v1", model="gpt-5.6"
        )
    }
    configured.models = {"openrouter": ModelProfileConfig(provider="openrouter", model="gpt-5.6")}
    configured.routing = {"builder": "openrouter"}
    save_global(configured)

    fresh = tmp_path / "brand-new"
    fresh.mkdir()
    app_instance = DainoApp(fresh)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, OnboardingScreen)
        scope = app_instance.screen.query_one("#settings-scope", Select)
        assert scope.value == "global"


@pytest.mark.asyncio
async def test_a_new_directory_still_asks_when_nothing_is_configured(
    tmp_path: Path,
) -> None:
    """Onboarding is skipped because the answer is known, not because it was removed."""
    fresh = tmp_path / "unconfigured"
    fresh.mkdir()
    app_instance = DainoApp(fresh)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, OnboardingScreen)


@pytest.mark.asyncio
async def test_the_terminal_client_inhibits_sleep_and_notifies_on_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn nobody is watching must keep running, and then say how it went.

    Driven through `_set_activity`, which every one of the five turn workers
    already calls — so this covers chat, plan, ask, team, and QA at once.
    """
    monkeypatch.setenv("DAINO_NOTIFY", "on")
    monkeypatch.setenv("DAINO_WAKELOCK", "on")
    # No real caffeinate and no real desktop notification, without touching the
    # shared `platform` module (which would silence the notifier as well).
    monkeypatch.setattr("daino.keepawake.shutil.which", lambda _: None)
    announced: list[tuple[str, str]] = []

    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        attention = workspace.missions.attention
        monkeypatch.setattr(
            attention.notifications,
            "send",
            lambda kind, title, body: announced.append((str(kind), body)),
        )

        workspace.verbose = True
        workspace._set_activity("building", "writing app.py")
        assert attention.keep_awake.active is True, "the host may sleep mid-turn"
        assert announced == [], "work in progress is not worth interrupting for"

        workspace._set_activity("completed", "all work verified")
        assert attention.keep_awake.active is False
        assert announced == [("completed", "all work verified")]

        # A failure after new work started notifies too, and only once.
        workspace._set_activity("verifying", "running checks")
        assert attention.keep_awake.active is True
        workspace._set_activity("failed", "tests failed")
        workspace._set_activity("idle")
        assert attention.keep_awake.active is False
        assert announced[-1] == ("failed", "tests failed")
        assert len(announced) == 2


@pytest.mark.asyncio
async def test_the_side_panel_lists_files_as_they_are_edited(tmp_path: Path) -> None:
    """"Which files has it touched so far?" must be answerable mid-turn.

    Otherwise the only record of the edits is the diff cards scrolling past in
    the transcript, and the answer is what a user checks before letting a long
    turn continue.
    """
    from daino.tui.widgets.checklist import TaskChecklist

    app_instance = initialized_app(tmp_path)
    async with app_instance.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app_instance.screen
        assert isinstance(workspace, WorkspaceScreen)
        checklist = workspace.query_one("#task-checklist", TaskChecklist)

        checklist.record_change("docs/index.html", 22, 37)
        checklist.record_change("README.md", 15, 15)
        # The same file edited twice is one row, summed.
        checklist.record_change("docs/index.html", 3, 1)
        await pilot.pause()

        assert checklist.changes == {"docs/index.html": (25, 38), "README.md": (15, 15)}
        painted = painted_text(app_instance)
        assert "EDITED" in painted
        assert "index.html" in painted
        assert "+40" in painted and "-53" in painted

        # A new turn starts from an empty list.
        checklist.clear_changes()
        await pilot.pause()
        assert checklist.changes == {}
        assert "EDITED" not in painted_text(app_instance)
