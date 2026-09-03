"""Multiline prompt with history and slash/reference suggestions."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, TextArea

from daino.tui import palette
from daino.tui.keybindings import SLASH_COMMANDS


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

    class PasteReceived(Message):
        def __init__(self, lines: int, characters: int) -> None:
            super().__init__()
            self.lines = lines
            self.characters = characters

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

    async def _on_paste(self, event: events.Paste) -> None:
        """Insert a bracketed paste as one normalized multiline edit."""
        normalized = event.text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        event.text = normalized
        await super()._on_paste(event)
        self.post_message(
            self.PasteReceived(
                lines=normalized.count("\n") + 1,
                characters=len(normalized),
            )
        )


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
        #: Project-defined commands, so a user's own ``/review-pr`` completes
        #: alongside the built-ins rather than being something only they know is
        #: there. Names as ``(name, hint)`` pairs, set by the workspace screen.
        self.custom_commands: list[tuple[str, str]] = []
        self._paste_received = False
        #: Last rendered completion set, so an unchanged one costs nothing.
        self._suggestions: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="prompt-header"):
            yield Label("message", id="prompt-title")
            yield Label("0 chars", id="prompt-meta")
        yield ListView(id="prompt-suggestions", classes="hidden")
        with Horizontal(id="prompt-row"):
            yield Label(f"[{palette.PROMPT_ACCENT}]›[/]", id="prompt-caret")
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

    def set_custom_commands(self, commands: list[tuple[str, str]]) -> None:
        self.custom_commands = commands

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
        self._paste_received = False
        self._paint_meta()
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

    def on_prompt_text_area_paste_received(self, _: PromptTextArea.PasteReceived) -> None:
        self._paste_received = True
        self._paint_meta()

    async def on_text_area_changed(self, _: TextArea.Changed) -> None:
        value = self.text
        if not value:
            self._paste_received = False
        self._paint_meta()
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
            # After the built-ins, mirroring the dispatcher: a project command
            # cannot shadow one of Daino's, so it should not appear to.
            suggestions.extend(
                (name, hint) for name, hint in self.custom_commands if name.startswith(token)
            )
        elif token.startswith("@"):
            builtins = ["@file:", "@symbol:", "@image:", "@mission:", "@playbook:"]
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
        value = area.text
        end = len(value)
        start = end
        while start > 0 and not value[start - 1].isspace():
            start -= 1
        suffix = " " if replacement.startswith("/") else ""
        area.load_text(value[:start] + replacement + suffix)
        self._hide_suggestions()
        area.focus()

    def _paint_meta(self) -> None:
        if not self.is_mounted:
            return
        value = self.text
        lines = value.count("\n") + 1 if value else 0
        parts = []
        if self._paste_received and value:
            parts.append("PASTED")
        if lines > 1:
            parts.append(f"{lines} lines")
        parts.append(f"{len(value):,} chars")
        self.query_one("#prompt-meta", Label).update(" · ".join(parts))

    def _show_suggestions(self) -> None:
        self.query_one("#prompt-suggestions", ListView).remove_class("hidden")
        self.add_class("suggestions-open")

    def _hide_suggestions(self) -> None:
        self.query_one("#prompt-suggestions", ListView).add_class("hidden")
        self.remove_class("suggestions-open")
