"""Minimal empty-conversation prompt."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from daino.tui import palette


class WelcomeBanner(Vertical):
    """A single quiet invitation; the persistent header already carries identity."""

    def __init__(self, provider: str, runtime: str) -> None:
        super().__init__(classes="welcome-card")
        self.provider = provider
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static(
            Content.assemble(
                ("› ", f"bold {palette.ACCENT}"),
                (
                    "Ask about the repository, describe a change, or type / for commands.",
                    palette.FAINT,
                ),
            ),
            id="welcome-help",
        )
