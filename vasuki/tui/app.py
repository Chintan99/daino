"""Textual application entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App
from textual.theme import Theme

from vasuki.application import ProjectContext, adopt_project, open_project
from vasuki.config import config_path, find_project_root
from vasuki.config.globals import has_global_provider
from vasuki.tui import palette
from vasuki.tui.screens import OnboardingScreen, WorkspaceScreen

VASUKI_DARK_THEME = Theme(
    name="vasuki-dark",
    primary=palette.ACCENT,
    secondary=palette.MUTED,
    accent=palette.ACCENT,
    warning=palette.CAUTION,
    error=palette.ALERT,
    success=palette.READY,
    foreground=palette.TEXT,
    background=palette.BACKGROUND,
    surface=palette.BACKGROUND,
    panel="#101012",
    boost="#16171a",
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
        "scrollbar": "#1f2023",
        "scrollbar-background": palette.BACKGROUND,
        "scrollbar-hover": "#2b2d31",
        "scrollbar-active": palette.MUTED,
        "footer-background": palette.BACKGROUND,
        "text-muted": palette.MUTED,
        "text-disabled": palette.FAINT,
    },
)


class VasukiApp(App[None]):
    """Persistent interactive workspace over Vasuki application services."""

    TITLE = "Vasuki"
    SUB_TITLE = "AI engineering workspace"
    CSS_PATH = "styles/vasuki.tcss"
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
        self.project = find_project_root(project)
        self.project_context = context

    async def on_mount(self) -> None:
        if self.project_context is not None:
            await self.open_workspace(self.project_context)
            return
        if not config_path(self.project).exists():
            # Asking again in every new directory is setup work, not a choice.
            # When a model is already configured globally there is nothing left
            # to ask: initialize quietly and open the workspace.
            if has_global_provider():
                try:
                    context = await asyncio.to_thread(adopt_project, self.project)
                except Exception as exc:  # noqa: BLE001 - fall back to asking
                    await self.push_screen(
                        OnboardingScreen(
                            self.project,
                            error=f"Could not set up this project automatically: {exc}",
                        )
                    )
                    return
                await self.open_workspace(context)
                return
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
                        "Repair .vasuki/config.yaml or reinitialize intentionally."
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
    """Launch Vasuki in the requested repository.

    Crash logging is installed before the app starts. A segmentation fault kills
    the interpreter with no traceback and nothing in the transcript, so without
    this the only evidence a user can report is the word "segfault".
    """
    from vasuki.config.loader import find_project_root
    from vasuki.utils import crashlog

    try:
        root = find_project_root(project)
    except Exception:  # noqa: BLE001 - diagnostics must not decide whether we start
        root = project or Path.cwd()
    crashlog.install(root)
    VasukiApp(project).run()
