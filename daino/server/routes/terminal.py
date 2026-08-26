"""Interactive terminal lifecycle (I/O streams over the WebSocket)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/terminals", tags=["terminal"])


@router.post("")
def create_terminal(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    session = state.terminals.create()
    return {"id": session.id, "cwd": session.cwd}


@router.get("")
def list_terminals(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    return {"terminals": state.terminals.list_ids()}


@router.delete("/{terminal_id}")
def close_terminal(state: Annotated[GuiState, Depends(get_state)], terminal_id: str) -> dict:
    closed = state.terminals.close(terminal_id)
    return {"id": terminal_id, "closed": closed}
