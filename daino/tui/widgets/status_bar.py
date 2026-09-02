"""Minimal workspace chrome: header, tabs, context, and key hints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from daino import __version__, branding
from daino.tui import palette
from daino.tui.render import join

#: Views promoted to the tab bar. Everything else stays reachable through the
#: command palette and slash commands, which is what "ctrl+p commands" advertises.
PRIMARY_TABS: tuple[tuple[str, str], ...] = (
    ("chat", "chat-view"),
    ("missions", "missions-view"),
    ("inspector", "inspector-view"),
    ("files", "files-view"),
    ("changes", "changes-view"),
    ("tests", "tests-view"),
    ("logs", "logs-view"),
    ("map", "map-view"),
)


def _shorten_path(path: str, home: str) -> str:
    if not home:
        return path
    normalized = home.rstrip("/")
    if path == normalized:
        return "~"
    prefix = f"{normalized}/"
    shortened = f"~/{path[len(prefix) :]}" if path.startswith(prefix) else path
    return f"…/{Path(path).name}" if len(shortened) > 34 else shortened


def _format_cost(cost: float) -> str:
    """Keep very small, real charges visible instead of rounding them to zero."""
    value = max(0.0, cost)
    if value == 0:
        return "$0.0000"
    if value < 0.0001:
        if value < 0.0000000001:
            return f"${value:.2e}"
        return f"${value:.10f}".rstrip("0")
    return f"${value:.4f}"


class DainoHeader(Vertical):
    """Two compact rows: workspace identity, then model and usage."""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="header-row"):
            yield Static("", id="header-identity", classes="header-left")
            yield Static("", id="header-state", classes="header-right")
        with Horizontal(classes="header-row header-detail-row"):
            yield Static("", id="header-environment", classes="header-left")
            yield Static("", id="header-usage", classes="header-right")

    def set_state(
        self,
        *,
        project: str,
        branch: str = "",
        model: str = "not configured",
        provider: str = "offline",
        runtime: str = "local",
        interaction_mode: str = "ask",
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
            (branding.NAME, f"bold {palette.ACCENT}"),
            (__version__, palette.DIM),
            (_shorten_path(project, home), palette.BRIGHT),
            *((branch, palette.MUTED),) if branch else (),
        )
        mode_colour = {
            "plan": palette.PLAN,
            "ask": palette.CAUTION,
            "session": palette.TOOL,
            "full": palette.ALERT,
        }.get(interaction_mode.casefold(), palette.MUTED)
        lowered_status = status.casefold()
        status_colour = (
            palette.ALERT
            if lowered_status in {"blocked", "cancelled", "failed"}
            else palette.CAUTION
            if lowered_status in {"awaiting approval", "planning", "thinking", "working"}
            else palette.READY
            if lowered_status in {"approved", "completed", "ready"}
            else palette.MUTED
        )
        state = join(
            "  ",
            (interaction_mode.upper(), mode_colour),
            Content.assemble(
                ("●", dot),
                (f" {lowered_status}", status_colour),
            ),
        )
        environment = join(
            "  ·  ",
            (model or "not configured", palette.TEXT),
            (provider or "offline", palette.MUTED),
            (runtime, palette.MUTED),
        )
        usage = join(
            "  ·  ",
            (f"{tokens:,} tok", palette.DIM),
            (_format_cost(cost), palette.DIM),
        )
        self.query_one("#header-identity", Static).update(identity)
        self.query_one("#header-state", Static).update(state)
        self.query_one("#header-environment", Static).update(environment)
        self.query_one("#header-usage", Static).update(usage)


class NavigationTab(Static):
    """A focusable tab that opens its view with click, Enter, or Space."""

    can_focus = True
    BINDINGS = [
        ("enter", "select", "Open"),
        ("space", "select", "Open"),
    ]

    class Selected(Message):
        def __init__(self, view_id: str) -> None:
            super().__init__()
            self.view_id = view_id

    def __init__(self, label: str, view_id: str, number: int = 0) -> None:
        super().__init__(label, id=f"tab-{view_id}", classes="nav-tab")
        self.number = number
        self.label = label
        self.view_id = view_id
        self.badge = ""
        self.active = False

    def set_state(self, *, active: bool | None = None, badge: str | None = None) -> None:
        if active is not None:
            self.active = active
        if badge is not None:
            self.badge = badge
        style = f"bold {palette.ACCENT}" if self.active else palette.MUTED
        badge_colour = (
            palette.ALERT
            if "fail" in self.badge.casefold()
            else palette.CAUTION
            if self.badge and self.badge != "done"
            else palette.READY
        )
        self.set_class(self.active, "active")
        tab_label = f" {self.label} "
        self.update(
            Content.assemble(
                (tab_label, style),
                *(((f" {self.badge}", badge_colour),) if self.badge and self.badge != "0" else ()),
            )
        )

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_select()

    def action_select(self) -> None:
        self.post_message(self.Selected(self.view_id))


class NavigationTabs(Horizontal):
    """One restrained row of primary workspace views."""

    def compose(self) -> ComposeResult:
        for number, (label, view_id) in enumerate(PRIMARY_TABS, 1):
            yield NavigationTab(label, view_id, number)
        yield Static("", classes="nav-spacer")
        yield Static(
            Content.assemble(("ctrl+p", palette.DIM), (" commands", palette.FAINT)),
            classes="nav-more",
        )

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
        verification_colour = (
            palette.READY
            if any(word in self.verification for word in ("passed", "verified"))
            else palette.ALERT
            if "failed" in self.verification
            else palette.CAUTION
        )
        mission = (
            Content.styled("no mission", palette.FAINT)
            if self.mission == "none"
            else Content.styled(self.mission, palette.VALUE)
        )
        files = Content.assemble(
            (str(self.attached), palette.VALUE),
            (" files", palette.FAINT),
        )
        verification = Content.assemble(
            ("tests ", palette.FAINT),
            (self.verification, verification_colour),
        )
        approvals = Content.assemble(
            (
                str(self.approvals),
                palette.ALERT if self.approvals else palette.VALUE,
            ),
            (" approvals", palette.FAINT),
        )

        self.update(
            join(
                "  ·  ",
                mission,
                files,
                verification,
                approvals,
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


class DainoHintBar(Static):
    """Key reference and an always-visible, colour-coded autonomy mode."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.submit = "enter"
        self.extra = ""
        self.mode = "ask"

    def set_state(
        self,
        *,
        submit: str = "enter",
        extra: str = "",
        mode: str = "ask",
    ) -> None:
        self.submit = submit
        self.extra = extra
        self.mode = mode
        self._paint()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._paint()

    def on_resize(self, _: events.Resize) -> None:
        self._paint()

    def _paint(self) -> None:
        def hint(key: str, action: str) -> Content:
            return Content.assemble((key, palette.FAINTEST), (f" {action}", palette.HINT))

        mode_colour = {
            "plan": palette.PLAN,
            "ask": palette.CAUTION,
            "session": palette.TOOL,
            "full": palette.ALERT,
        }.get(self.mode.casefold(), palette.MUTED)
        hints = (
            (
                hint(self.submit, "send"),
                hint("shift+tab", "mode"),
                hint("ctrl+p", "palette"),
            )
            if 0 < self.size.width < 80
            else (
                hint(self.submit, "send"),
                hint("shift+enter", "newline"),
                hint("shift+tab", "mode"),
                hint("ctrl+p", "palette"),
                hint("/", "commands"),
                hint("?", "help"),
                hint("esc", "cancel"),
            )
        )
        self.update(
            join(
                "   ",
                (
                    self.mode.upper(),
                    f"bold {mode_colour}",
                ),
                *hints,
                (self.extra, palette.FAINTEST),
            )
        )
