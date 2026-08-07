"""Workspace chrome: header row, navigation tabs, context strip, hint bar.

The design is a flat terminal surface, so none of these are panels. Each is a
single row of space-separated tokens whose colour carries the hierarchy.
"""

from __future__ import annotations

from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from vasuki import __version__
from vasuki.tui import palette
from vasuki.tui.render import join

#: Views promoted to the tab bar. Everything else stays reachable through the
#: command palette and slash commands, which is what "ctrl+p more" advertises.
PRIMARY_TABS: tuple[tuple[str, str], ...] = (
    ("chat", "chat-view"),
    ("missions", "missions-view"),
    ("files", "files-view"),
    ("changes", "changes-view"),
    ("tests", "tests-view"),
    ("logs", "logs-view"),
)


def _shorten_path(path: str, home: str) -> str:
    return f"~{path[len(home) :]}" if home and path.startswith(home) else path


class VasukiHeader(Horizontal):
    """Top row: identity on the left, live session vitals pushed to the right."""

    def compose(self) -> ComposeResult:
        yield Static("", id="header-identity")
        yield Static("", id="header-vitals")

    def set_state(
        self,
        *,
        project: str,
        branch: str = "",
        model: str = "not configured",
        provider: str = "offline",
        runtime: str = "local",
        status: str = "ready",
        connected: bool | None = None,
        tokens: int = 0,
        cost: float = 0.0,
        home: str = "",
    ) -> None:
        dot = (
            palette.READY
            if connected is True
            else palette.ALERT
            if connected is False
            else palette.FAINT
        )
        identity = join(
            "  ",
            ("vasuki", f"bold {palette.ACCENT}"),
            (__version__, palette.DIM),
            (_shorten_path(project, home), palette.DIM),
            *(((branch, palette.DIM),) if branch else ()),
            (provider, palette.MUTED),
            (model, palette.MUTED),
            (runtime, palette.MUTED),
        )
        vitals = join(
            "  ",
            Content.assemble(("●", dot), (f" {status.casefold()}", palette.MUTED)),
            (f"{tokens:,} tok", palette.DIM),
            (f"${cost:.4f}", palette.DIM),
        )
        self.query_one("#header-identity", Static).update(identity)
        self.query_one("#header-vitals", Static).update(vitals)


class NavigationTab(Static):
    """One tab. Clicking it opens the matching view."""

    class Selected(Message):
        def __init__(self, view_id: str) -> None:
            super().__init__()
            self.view_id = view_id

    def __init__(self, label: str, view_id: str) -> None:
        super().__init__(label, id=f"tab-{view_id}", classes="nav-tab")
        self.label = label
        self.view_id = view_id
        self.badge = ""
        self.active = False

    def set_state(self, *, active: bool | None = None, badge: str | None = None) -> None:
        if active is not None:
            self.active = active
        if badge is not None:
            self.badge = badge
        colour = palette.ACCENT if self.active else palette.MUTED
        style = f"bold {colour}" if self.active else colour
        self.set_class(self.active, "active")
        self.update(
            Content.assemble(
                (self.label, style),
                *(((f" {self.badge}", palette.DIM),) if self.badge else ()),
            )
        )

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Selected(self.view_id))


class NavigationTabs(Horizontal):
    """Horizontal tab strip replacing the old vertical sidebar."""

    def compose(self) -> ComposeResult:
        for label, view_id in PRIMARY_TABS:
            yield NavigationTab(label, view_id)
        yield Static("", classes="nav-spacer")
        yield Static(f"[{palette.FAINT}]ctrl+p  more[/]", classes="nav-more")

    def on_mount(self) -> None:
        self.set_active("chat-view")

    def set_active(self, view_id: str) -> None:
        for tab in self.query(NavigationTab):
            tab.set_state(active=tab.view_id == view_id)

    def set_badges(self, badges: dict[str, str]) -> None:
        for tab in self.query(NavigationTab):
            if tab.view_id in badges:
                tab.set_state(badge=badges[tab.view_id])


class ContextStrip(Static):
    """One dim row of session facts, sitting just above the prompt."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.mission = "none"
        self.attached = 0
        self.verification = "not run"
        self.approvals = 0
        self.activity = ""

    def _paint(self) -> None:
        def item(label: str, value: str) -> Content:
            return Content.assemble((label, palette.FAINT), (f" {value}", palette.VALUE))

        self.update(
            join(
                "   ",
                item("mission", self.mission),
                item("attached", str(self.attached)),
                item("verify", self.verification),
                item("approvals", str(self.approvals)),
                (self.activity, palette.FAINT),
            )
        )

    def set_mission(self, mission_id: str, status: str) -> None:
        self.mission = f"{mission_id} {status.casefold()}" if mission_id else "none"
        self._paint()

    def set_tests(self, text: str) -> None:
        self.verification = text.casefold()
        self._paint()

    def set_approvals(self, count: int) -> None:
        self.approvals = count
        self._paint()

    def set_files(self, paths: list[str]) -> None:
        self.attached = len(paths)
        self._paint()

    def add_activity(self, line: str) -> None:
        self.activity = line.strip()
        self._paint()


class VasukiHintBar(Static):
    """Static key reference along the bottom edge."""

    def set_state(self, *, submit: str = "enter", extra: str = "") -> None:
        def hint(key: str, action: str) -> Content:
            return Content.assemble((key, palette.FAINTEST), (f" {action}", palette.HINT))

        self.update(
            join(
                "   ",
                hint(submit, "send"),
                hint("shift+enter", "newline"),
                hint("/", "commands"),
                hint("@", "files"),
                hint("!", "shell"),
                hint("esc", "cancel"),
                (extra, palette.FAINTEST),
            )
        )
