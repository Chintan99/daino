from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual import events as textual_events
from textual.containers import VerticalScroll
from textual.widgets import ContentSwitcher, DataTable, Input, ListView, Select
from typer.testing import CliRunner

from tests.conftest import commit_all, painted_text
from vasuki.application import (
    ProviderApplicationService,
    initialize_project,
    open_project,
)
from vasuki.application.view_models import OpenRouterModel
from vasuki.cli.app import app
from vasuki.events import (
    ApprovalRequested,
    DeploymentProgress,
    FileChanged,
    MissionCompleted,
    ModelStreamChunk,
    ToolCompleted,
    ToolFailed,
    ToolStarted,
)
from vasuki.events import (
    TestsCompleted as VerificationCompletedEvent,
)
from vasuki.events import (
    TestsStarted as VerificationStartedEvent,
)
from vasuki.schemas import InteractionMode, QAReport
from vasuki.tui.app import VasukiApp
from vasuki.tui.keybindings import SLASH_COMMANDS
from vasuki.tui.screens.onboarding import OnboardingScreen
from vasuki.tui.screens.views import ProvidersView, QAView
from vasuki.tui.screens.workspace import WorkspaceScreen
from vasuki.tui.widgets import (
    ApprovalModal,
    CommandPalette,
    ConversationView,
    NavigationTab,
    NavigationTabs,
    PromptInput,
    TaskChecklist,
    VasukiHintBar,
)
from vasuki.tui.widgets.message import MessageCard
from vasuki.tui.widgets.prompt_input import PromptTextArea


def initialized_app(root: Path) -> VasukiApp:
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    commit_all(root)
    initialize_project(root)
    return VasukiApp(root, context=open_project(root))


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
        assert "VASUKI" in painted
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
            "chat-view",
        ):
            assert await pilot.click(f"#tab-{view_id}")
            await pilot.pause()
            assert switcher.current == view_id
            assert [tab.view_id for tab in workspace.query(NavigationTab) if tab.active] == [
                view_id
            ]


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
        hint = workspace.query_one("#hint-bar", VasukiHintBar).render()
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
    app_instance = VasukiApp(tmp_path)
    async with app_instance.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, OnboardingScreen)
        assert app_instance.screen.query_one("#initialize")


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
        hint = workspace.query_one("#hint-bar", VasukiHintBar)

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
    app_instance = VasukiApp(tmp_path, context=context)
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
    second_app = VasukiApp(tmp_path, context=second_context)
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
    monkeypatch.setattr("vasuki.tui.run_tui", calls.append)
    runner = CliRunner()

    result = runner.invoke(app, ["--project", str(tmp_path)])
    explicit = runner.invoke(app, ["tui", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert explicit.exit_code == 0
    assert calls == [tmp_path.resolve(), tmp_path]


@pytest.mark.asyncio
async def test_a_new_directory_opens_straight_to_work_when_a_model_is_configured(
    tmp_path: Path,
) -> None:
    """The reported problem: every new folder asked for configuration again."""
    from vasuki.config.globals import save_global
    from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings

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
    app_instance = VasukiApp(fresh)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app_instance.screen, WorkspaceScreen):
                break

        # Straight to the workspace: no form, no questions.
        assert isinstance(app_instance.screen, WorkspaceScreen)
        assert not isinstance(app_instance.screen, OnboardingScreen)
        # And the globally configured model is the one it will use.
        assert "openrouter" in app_instance.screen.context.settings.providers


@pytest.mark.asyncio
async def test_a_new_directory_still_asks_when_nothing_is_configured(
    tmp_path: Path,
) -> None:
    """Onboarding is skipped because the answer is known, not because it was removed."""
    fresh = tmp_path / "unconfigured"
    fresh.mkdir()
    app_instance = VasukiApp(fresh)
    async with app_instance.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        assert isinstance(app_instance.screen, OnboardingScreen)
