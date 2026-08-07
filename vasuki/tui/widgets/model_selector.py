"""Session-scoped model selector."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class ModelSelector(ModalScreen[str | None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, models: list[dict[str, object]]) -> None:
        super().__init__()
        self.models = models

    def compose(self) -> ComposeResult:
        with Vertical(id="model-dialog"):
            yield Label("Select model for this session", classes="modal-title")
            if self.models:
                yield OptionList(
                    *[
                        Option(
                            f"[b]{item['name']}[/b]  {item['model']}\n"
                            f"[dim]{item['provider']} · {item['context_window']} tokens · "
                            f"{item['cost']} cost[/dim]",
                            id=str(item["name"]),
                        )
                        for item in self.models
                    ],
                    id="model-options",
                )
            else:
                yield Label(
                    "No models are configured. Open Providers or run `vasuki providers add …`."
                )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_close(self) -> None:
        self.dismiss(None)
