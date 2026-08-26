"""Session, message, and context-bar HTTP endpoints.

Sending a message and streaming a turn happen over the WebSocket
(``/ws/session/{id}``); these REST endpoints cover session listing/creation,
transcript loading, and context-file management, and are shared verbatim with
the TUI's application service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api", tags=["agent"])


class CreateSessionRequest(BaseModel):
    title: str = "New session"


class ContextFileRequest(BaseModel):
    path: str


@router.get("/sessions")
def list_sessions(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    sessions = state.missions.recent_sessions(limit=50)
    return {
        "sessions": [
            {
                "id": item.id,
                "title": item.title,
                "active_model": item.active_model,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                "context_files": list(item.context_files),
            }
            for item in sessions
        ]
    }


@router.post("/sessions")
def create_session(
    state: Annotated[GuiState, Depends(get_state)], body: CreateSessionRequest
) -> dict:
    session_id = state.missions.create_session(body.title)
    return {"id": session_id, "title": body.title}


@router.get("/sessions/latest")
def latest_session(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    return {"id": state.missions.latest_session()}


@router.get("/sessions/{session_id}/messages")
def messages(state: Annotated[GuiState, Depends(get_state)], session_id: str) -> dict:
    items = state.missions.messages(session_id)
    return {
        "session_id": session_id,
        "messages": [
            {
                "id": item.id,
                "kind": item.kind,
                "role": item.role,
                "content": item.content,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "metadata": item.metadata,
            }
            for item in items
        ],
    }


@router.get("/sessions/{session_id}/todos")
def todos(state: Annotated[GuiState, Depends(get_state)], session_id: str) -> dict:
    items = state.missions.session_todos(session_id)
    return {
        "session_id": session_id,
        "todos": [item.model_dump(mode="json") for item in items],
    }


@router.get("/sessions/{session_id}/context")
def context_files(state: Annotated[GuiState, Depends(get_state)], session_id: str) -> dict:
    try:
        return {"session_id": session_id, "files": state.missions.context_files(session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/context")
def toggle_context(
    state: Annotated[GuiState, Depends(get_state)],
    session_id: str,
    body: ContextFileRequest,
) -> dict:
    try:
        attached = state.missions.toggle_context_file(session_id, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": body.path, "attached": attached}


@router.get("/workspace")
def workspace(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    settings = state.context.settings
    return {
        "name": settings.project.name,
        "root": str(state.root),
        "runtime": settings.runtime.default,
        "models": sorted(settings.models.keys()),
        "routing": dict(settings.routing),
    }
