"""Local FastAPI + WebSocket backend for the Daino browser IDE.

The server is a thin transport over the same runtime the TUI uses: it constructs
a :class:`~daino.application.context.ProjectContext` and
:class:`~daino.application.mission_service.MissionApplicationService`, bridges the
existing :class:`~daino.events.EventBus` to WebSockets, and supplies a
socket-backed approval callback. No agent logic lives here.
"""

from daino.server.app import create_app
from daino.server.launch import run_gui
from daino.server.state import GuiState

__all__ = ["GuiState", "create_app", "run_gui"]
