"""Multiline prompt with history and slash/reference suggestions."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, TextArea

from vasuki.tui import palette
from vasuki.tui.keybindings import SLASH_COMMANDS


class PromptTextArea(TextArea):
    class SubmitRequested(Message):
        pass

    class CancelRequested(Message):
        pass

    class HistoryRequested(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class SuggestionFocusRequested(Message):
        pass

    class ChatScrollRequested(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    def __init__(
        self,
        text: str = "",
        *,
        id: str | None = None,
        language: str | None = None,
        soft_wrap: bool = True,
        show_line_numbers: bool = False,
    ) -> None:
        super().__init__(
            text,
            id=id,
            language=language,
            soft_wrap=soft_wrap,
            show_line_numbers=show_line_numbers,
        )

    #: Enter sends. Nothing else does, so the key that sends a prompt is the one
    #: every chat interface uses and never has to be discovered or configured.
    SUBMIT_KEYS = frozenset({"enter"})
    #: Shift+Enter is the newline everywhere; Alt+Enter is kept for terminals
    #: that swallow Shift+Enter before the application sees it.
    NEWLINE_KEYS = frozenset({"shift+enter", "alt+enter"})

    @property
    def submit_keys(self) -> set[str]:
        return set(self.SUBMIT_KEYS)

    @property
    def newline_keys(self) -> set[str]:
        return set(self.NEWLINE_KEYS)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in self.newline_keys:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key in self.submit_keys:
            event.prevent_default()
            event.stop()
            self.post_message(self.SubmitRequested())
            return
        if event.key in {"pageup", "pagedown"}:
            event.prevent_default()
            event.stop()
            self.post_message(self.ChatScrollRequested(-1 if event.key == "pageup" else 1))
            return
        if event.key == "escape":
            self.post_message(self.CancelRequested())
        if "\n" not in self.text and event.key in {"up", "down"}:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryRequested(-1 if event.key == "up" else 1))
            return
        if event.key == "tab":
            self.post_message(self.SuggestionFocusRequested())
            return
        await super()._on_key(event)


class PromptInput(Vertical):
    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Cancelled(Message):
        pass

    class ChatScrollRequested(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    def __init__(self) -> None:
        super().__init__(id="prompt-area")
        self.history: list[str] = []
        self.history_index = 0
        self.references: list[str] = []
        #: Last rendered completion set, so an unchanged one costs nothing.
        self._suggestions: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield ListView(id="prompt-suggestions", classes="hidden")
        with Horizontal(id="prompt-row"):
            yield Label(f"[{palette.ACCENT}]❯[/]", id="prompt-caret")
            yield PromptTextArea(
                "",
                id="prompt",
                language=None,
                soft_wrap=True,
                show_line_numbers=False,
            )

    @property
    def text(self) -> str:
        return self.query_one("#prompt", PromptTextArea).text

    def set_references(self, references: list[str]) -> None:
        self.references = references

    def focus_prompt(self) -> None:
        self.query_one("#prompt", PromptTextArea).focus()

    async def on_prompt_text_area_submit_requested(self, _: PromptTextArea.SubmitRequested) -> None:
        value = self.text.strip()
        if not value:
            return
        suggestions = self.query_one("#prompt-suggestions", ListView)
        selected = suggestions.highlighted_child
        if (
            not suggestions.has_class("hidden")
            and selected is not None
            and selected.name
            and value != selected.name
        ):
            self._apply_suggestion(selected.name)
            return
        self.history.append(value)
        self.history_index = len(self.history)
        area = self.query_one("#prompt", PromptTextArea)
        area.load_text("")
        self._hide_suggestions()
        self.post_message(self.Submitted(value))

    def on_prompt_text_area_cancel_requested(self, _: PromptTextArea.CancelRequested) -> None:
        suggestions = self.query_one("#prompt-suggestions", ListView)
        if not suggestions.has_class("hidden"):
            self._hide_suggestions()
            self.focus_prompt()
            return
        self.post_message(self.Cancelled())

    def on_prompt_text_area_history_requested(self, event: PromptTextArea.HistoryRequested) -> None:
        suggestions = self.query_one("#prompt-suggestions", ListView)
        if not suggestions.has_class("hidden") and suggestions.children:
            current = suggestions.index if suggestions.index is not None else 0
            suggestions.index = max(
                0,
                min(len(suggestions.children) - 1, current + event.direction),
            )
            return
        if not self.history:
            return
        self.history_index = max(
            0,
            min(len(self.history), self.history_index + event.direction),
        )
        value = "" if self.history_index == len(self.history) else self.history[self.history_index]
        self.query_one("#prompt", PromptTextArea).load_text(value)

    def on_prompt_text_area_suggestion_focus_requested(
        self, _: PromptTextArea.SuggestionFocusRequested
    ) -> None:
        suggestions = self.query_one("#prompt-suggestions", ListView)
        if not suggestions.has_class("hidden") and suggestions.children:
            suggestions.index = 0
            suggestions.focus()

    def on_prompt_text_area_chat_scroll_requested(
        self, event: PromptTextArea.ChatScrollRequested
    ) -> None:
        self.post_message(self.ChatScrollRequested(event.direction))

    async def on_text_area_changed(self, _: TextArea.Changed) -> None:
        value = self.text
        token = (
            "" if value.endswith((" ", "\n", "\t")) else value.split()[-1] if value.split() else ""
        )
        suggestions: list[tuple[str, str]] = []
        if token.startswith("/"):
            suggestions = [
                (
                    item.name,
                    f"{item.usage}  {item.description}".strip(),
                )
                for item in SLASH_COMMANDS
                if item.name.startswith(token)
            ]
        elif token.startswith("@"):
            builtins = ["@file:", "@symbol:", "@mission:", "@playbook:"]
            values = [*builtins, *self.references]
            suggestions = [(item, "Attach reference") for item in values if item.startswith(token)][
                :8
            ]
        # Ordinary prose is almost every keystroke. Rebuilding a hidden list each
        # time cost a widget clear and a refresh per character typed.
        if suggestions == self._suggestions:
            return
        self._suggestions = suggestions
        view = self.query_one("#prompt-suggestions", ListView)
        await view.clear()
        if not suggestions:
            self._hide_suggestions()
            return
        self._show_suggestions()
        await view.extend(
            [
                ListItem(Label(f"[b]{name}[/b]  [dim]{description}[/dim]"), name=name)
                for name, description in suggestions
            ]
        )
        view.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "prompt-suggestions" or event.item is None:
            return
        self._apply_suggestion(event.item.name or "")

    def _apply_suggestion(self, replacement: str) -> None:
        if not replacement:
            return
        area = self.query_one("#prompt", PromptTextArea)
        parts = area.text.split()
        if parts:
            parts[-1] = replacement
        else:
            parts = [replacement]
        area.load_text(" ".join(parts) + (" " if replacement.startswith("/") else ""))
        self._hide_suggestions()
        area.focus()

    def _show_suggestions(self) -> None:
        self.query_one("#prompt-suggestions", ListView).remove_class("hidden")
        self.add_class("suggestions-open")

    def _hide_suggestions(self) -> None:
        self.query_one("#prompt-suggestions", ListView).add_class("hidden")
        self.remove_class("suggestions-open")
