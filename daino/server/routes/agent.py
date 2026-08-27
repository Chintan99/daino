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
    #: Empty means unnamed: the service's placeholder applies and the first
    #: request the session receives renames it.
    title: str = ""


class ContextFileRequest(BaseModel):
    path: str


class SessionModelRequest(BaseModel):
    #: Empty means "auto": follow the saved routing, and allow escalation.
    profile: str = ""


@router.get("/sessions")
def list_sessions(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    sessions = state.missions.recent_sessions(limit=50)
    counts = state.missions.session_message_counts()
    return {
        "sessions": [
            {
                "id": item.id,
                "title": item.title,
                "active_model": item.active_model,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                "context_files": list(item.context_files),
                # Shown in the picker: a long session carries its whole history
                # into every prompt, which is worth being able to see.
                "message_count": counts.get(item.id, 0),
            }
            for item in sessions
        ]
    }


@router.post("/sessions")
def create_session(
    state: Annotated[GuiState, Depends(get_state)], body: CreateSessionRequest
) -> dict:
    title = body.title.strip()
    session_id = (
        state.missions.create_session(title) if title else state.missions.create_session()
    )
    return {"id": session_id, "title": title}


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


@router.post("/sessions/{session_id}/model")
def select_session_model(
    state: Annotated[GuiState, Depends(get_state)],
    session_id: str,
    body: SessionModelRequest,
) -> dict:
    """Pin a model profile to this session, the way the TUI's /model does.

    Sending the profile with each message alone left the session's stored model
    untouched, so the same conversation reopened in the terminal client showed a
    different model than the browser had been using.
    """
    try:
        if body.profile.strip():
            state.providers.select_for_session(session_id, body.profile)
        else:
            # Auto: the router picks per role, and a stalled turn may escalate to
            # a stronger model. A pinned session never can.
            state.providers.unpin_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": session_id, "profile": body.profile.strip()}


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
