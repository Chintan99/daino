"""Textual application entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App
from textual.theme import Theme

from daino import branding
from daino.application import ProjectContext, open_project
from daino.config import config_path, find_project_root
from daino.tui import palette
from daino.tui.screens import OnboardingScreen, WorkspaceScreen

VASUKI_DARK_THEME = Theme(
    name="daino-dark",
    primary=palette.ACCENT,
    secondary=palette.MUTED,
    accent=palette.ACCENT,
    warning=palette.CAUTION,
    error=palette.ALERT,
    success=palette.READY,
    foreground=palette.TEXT,
    background=palette.BACKGROUND,
    surface=palette.BACKGROUND,
    panel=palette.SURFACE,
    boost=palette.SURFACE_BRIGHT,
    dark=True,
    variables={
        "block-cursor-background": palette.INPUT,
        "block-cursor-foreground": palette.BACKGROUND,
        "block-cursor-text-style": "none",
        "button-color-foreground": palette.TEXT,
        "input-cursor-background": palette.INPUT,
        "input-cursor-foreground": palette.BACKGROUND,
        "input-selection-background": f"{palette.MUTED} 35%",
        "border": palette.RULE,
        "border-blurred": palette.RULE,
        "scrollbar": palette.RULE,
        "scrollbar-background": palette.BACKGROUND,
        "scrollbar-hover": palette.SURFACE_BRIGHT,
        "scrollbar-active": palette.MUTED,
        "footer-background": palette.BACKGROUND,
        "text-muted": palette.MUTED,
        "text-disabled": palette.FAINT,
    },
)


class DainoApp(App[None]):
    """Persistent interactive workspace over Daino application services."""

    TITLE = branding.NAME
    SUB_TITLE = "AI engineering workspace"
    CSS_PATH = "styles/daino.tcss"
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        project: Path | None = None,
        *,
        context: ProjectContext | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.register_theme(VASUKI_DARK_THEME)
        # Starting the TUI in a directory is an explicit workspace choice. Do
        # not let a parent Git repository capture it and expose that parent's
        # conversation history and usage totals.
        self.project = find_project_root(project or Path.cwd())
        self.project_context = context

    async def on_mount(self) -> None:
        if self.project_context is not None:
            await self.open_workspace(self.project_context)
            return
        if not config_path(self.project).exists():
            await self.push_screen(OnboardingScreen(self.project))
            return
        try:
            context = open_project(self.project)
        except Exception as exc:
            await self.push_screen(
                OnboardingScreen(
                    self.project,
                    error=(
                        f"Existing configuration could not be loaded: {exc}. "
                        "Repair .daino/config.yaml or reinitialize intentionally."
                    ),
                )
            )
            return
        await self.open_workspace(context)

    async def open_workspace(self, context: ProjectContext) -> None:
        if self.project_context is not None and self.project_context is not context:
            self.project_context.close()
        self.project_context = context
        self.project = context.root
        await self.push_screen(WorkspaceScreen(context))

    def on_unmount(self) -> None:
        if self.project_context is not None:
            self.project_context.close()


def run_tui(project: Path | None = None) -> None:
    """Launch Daino in the requested repository.

    Crash logging is installed before the app starts. A segmentation fault kills
    the interpreter with no traceback and nothing in the transcript, so without
    this the only evidence a user can report is the word "segfault".
    """
    from daino.config.loader import find_project_root
    from daino.utils import crashlog

    try:
        root = find_project_root(project or Path.cwd())
    except Exception:  # noqa: BLE001 - diagnostics must not decide whether we start
        root = project or Path.cwd()
    crashlog.install(root)
    DainoApp(project).run()
