"""Git inspection and staging for the GUI (status, diff, stage, discard).

Never commits or pushes — the agent's own workflow owns that. What the GUI adds
here is the review surface: whole-file "before" and "after" content so the diff
renders side by side the way an editor shows it, rather than as a wall of hunks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from daino.events import GitChanged
from daino.exceptions import WorkspaceError
from daino.server.deps import get_state, language_for, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/git", tags=["git"])

#: A blob larger than this is reported as too big rather than shipped to Monaco.
_MAX_DIFF_BYTES = 2_000_000


class PathsRequest(BaseModel):
    paths: list[str]


def _parse_porcelain(text: str) -> dict[str, list[dict[str, str]]]:
    staged: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    untracked: list[dict[str, str]] = []
    for line in text.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status, path = line[0], line[1], line[3:]
        # Renames arrive as "old -> new"; the new path is the one to act on.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        entry = {"path": path}
        if index_status == "?" and worktree_status == "?":
            untracked.append(entry)
            continue
        if index_status not in (" ", "?"):
            staged.append({"path": path, "status": index_status})
        if worktree_status not in (" ", "?"):
            modified.append({"path": path, "status": worktree_status})
    return {"staged": staged, "modified": modified, "untracked": untracked}


def _blob(state: GuiState, revision: str, path: str) -> tuple[str, bool]:
    """Read one blob at ``revision``; returns (text, exists)."""
    result = state.git.run("show", f"{revision}:{path}", check=False)
    if not result.succeeded:
        return "", False
    return result.stdout, True


def _worktree_text(state: GuiState, path: str) -> tuple[str, bool, bool]:
    """Read the working-tree file; returns (text, exists, binary)."""
    target = safe_path(state, path)
    if not target.is_file():
        return "", False, False
    if target.stat().st_size > _MAX_DIFF_BYTES:
        return "", True, True
    try:
        return target.read_text(encoding="utf-8"), True, False
    except (UnicodeDecodeError, OSError):
        return "", True, True


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


@router.get("/file")
def file_diff(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(...),
    staged: bool = Query(default=False),
) -> dict:
    """Whole-file ``original`` and ``modified`` content for one changed path.

    Staged view compares HEAD against the index; the working view compares the
    index (falling back to HEAD for a file that was never staged) against what
    is on disk. Returning full files rather than hunks is what lets the editor
    show surrounding context and let the reader scroll through the file.
    """
    if not state.git.is_repository():
        return {
            "repository": False,
            "path": path,
            "staged": staged,
            "original": "",
            "modified": "",
            "language": "plaintext",
            "binary": False,
        }

    binary = False
    if staged:
        original, _ = _blob(state, "HEAD", path)
        modified, present = _blob(state, "", path)  # ":path" — the index
        if not present:
            modified, _ = _blob(state, "HEAD", path)
    else:
        original, present = _blob(state, "", path)
        if not present:
            original, _ = _blob(state, "HEAD", path)
        modified, _, binary = _worktree_text(state, path)

    if "\x00" in original or "\x00" in modified:
        binary = True
    if binary:
        original = modified = ""

    return {
        "repository": True,
        "path": path,
        "staged": staged,
        "original": original,
        "modified": modified,
        "language": language_for(state.root / path),
        "binary": binary,
    }


@router.post("/stage")
def stage(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    try:
        state.git.run("add", "--", *body.paths)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"staged": body.paths}


@router.post("/unstage")
def unstage(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    # "restore --staged" fails on a repository with no commits; reset works in both.
    result = state.git.run("reset", "-q", "HEAD", "--", *body.paths, check=False)
    if not result.succeeded:
        raise HTTPException(status_code=400, detail=result.stderr.strip() or "Unstage failed")
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"unstaged": body.paths}


@router.post("/discard")
def discard(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    """Throw away working-tree changes for tracked paths. Untracked files are left alone."""
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    result = state.git.run("checkout", "--", *body.paths, check=False)
    if not result.succeeded:
        raise HTTPException(status_code=400, detail=result.stderr.strip() or "Discard failed")
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"discarded": body.paths}


@router.get("/log")
def log(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "entries": []}
    lines = [line for line in state.git.log(limit).splitlines() if line.strip()]
    return {"repository": True, "entries": lines}
