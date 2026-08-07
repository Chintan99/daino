"""Searchable command palette."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from vasuki.tui.keybindings import COMMAND_PALETTE


class CommandPalette(ModalScreen[str | None]):
    BINDINGS = [("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="command-dialog"):
            yield Input(placeholder="Search commands…", id="command-search")
            yield OptionList(
                *[
                    Option(f"{label}  [dim]{command}[/dim]", id=command)
                    for label, command in COMMAND_PALETTE
                ],
                id="command-options",
            )

    def on_mount(self) -> None:
        self.query_one("#command-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.lower()
        options = self.query_one("#command-options", OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(f"{label}  [dim]{command}[/dim]", id=command)
                for label, command in COMMAND_PALETTE
                if query in label.lower() or query in command.lower()
            ]
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_close(self) -> None:
        self.dismiss(None)
