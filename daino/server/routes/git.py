"""Read-only Git inspection for the GUI (status, diff, branch, log).

Never commits or pushes — the agent's own workflow handles that, and the GUI
only surfaces what changed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/git", tags=["git"])


def _parse_porcelain(text: str) -> dict[str, list[dict[str, str]]]:
    staged: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    untracked: list[dict[str, str]] = []
    for line in text.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status, path = line[0], line[1], line[3:]
        entry = {"path": path}
        if index_status == "?" and worktree_status == "?":
            untracked.append(entry)
            continue
        if index_status not in (" ", "?"):
            staged.append({"path": path, "status": index_status})
        if worktree_status not in (" ", "?"):
            modified.append({"path": path, "status": worktree_status})
    return {"staged": staged, "modified": modified, "untracked": untracked}


@router.get("/status")
def status(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "branch": "", "staged": [], "modified": [], "untracked": []}
    parsed = _parse_porcelain(state.git.status(porcelain=True))
    return {"repository": True, "branch": state.git.current_branch(), **parsed}


@router.get("/diff")
def diff(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(default=""),
    staged: bool = Query(default=False),
) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "diff": ""}
    refs = (path,) if path else ()
    text = state.git.diff(*refs, staged=staged)
    return {"repository": True, "path": path, "staged": staged, "diff": text}


@router.get("/log")
def log(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "entries": []}
    lines = [line for line in state.git.log(limit).splitlines() if line.strip()]
    return {"repository": True, "entries": lines}
