"""Shared, process-wide services for one GUI-served project."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from daino.application.context import ProjectContext
from daino.application.mission_service import MissionApplicationService
from daino.design import DesignService
from daino.git import GitClient
from daino.services import PreviewManager, TerminalManager
from daino.tools.filesystem import FileTools


@dataclass
class GuiState:
    """Everything the GUI routes and WebSockets need for one open project.

    Built once at server start from a ``ProjectContext`` so every request and
    socket shares the same agent runtime, event bus, and session store.
    """

    context: ProjectContext
    missions: MissionApplicationService
    design: DesignService
    terminals: TerminalManager
    preview: PreviewManager
    files: FileTools
    git: GitClient
    #: Serialized so only one agentic turn runs against the shared runtime at a time.
    active_turns: dict[str, object] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.context.root

    @classmethod
    def from_context(cls, context: ProjectContext) -> GuiState:
        return cls(
            context=context,
            missions=MissionApplicationService(context),
            design=DesignService(context.root, events=context.events),
            terminals=TerminalManager(context.root),
            preview=PreviewManager(context.root),
            files=FileTools(context.root),
            git=GitClient(context.root),
        )

    def shutdown(self) -> None:
        self.terminals.close_all()
        self.preview.stop()
