"""Filesystem browsing and editing with optimistic-concurrency conflict checks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from daino.config import paths
from daino.events import GitChanged
from daino.server.deps import content_hash, get_state, language_for, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/files", tags=["files"])

_IGNORED = {".git", *paths.STATE_DIR_NAMES, ".venv", "venv", "node_modules", "__pycache__",
            ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}
_MAX_EDITABLE_BYTES = 2_000_000


class WriteRequest(BaseModel):
    path: str
    content: str
    #: sha256 of the content the client last read; guards against clobbering an
    #: out-of-band change. Omit/empty for a brand-new file.
    base_hash: str = ""


class CreateRequest(BaseModel):
    path: str
    is_dir: bool = False


class RenameRequest(BaseModel):
    source: str
    dest: str


@router.get("/tree")
def tree(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(default=""),
) -> dict:
    """List immediate children of a directory (lazy, one level at a time)."""
    directory = safe_path(state, path or ".", must_exist=True)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in _IGNORED:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(state.root)),
                "type": "directory" if child.is_dir() else "file",
            }
        )
    return {"path": path, "entries": entries}


@router.get("/read")
def read_file(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(...),
) -> dict:
    target = safe_path(state, path, must_exist=True)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    stat = target.stat()
    if stat.st_size > _MAX_EDITABLE_BYTES:
        raise HTTPException(status_code=413, detail="File is too large to open in the editor")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Binary or non-UTF-8 file") from exc
    return {
        "path": path,
        "content": content,
        "hash": content_hash(content),
        "mtime": stat.st_mtime,
        "language": language_for(target),
        "size": stat.st_size,
    }


@router.put("/write")
def write_file(state: Annotated[GuiState, Depends(get_state)], body: WriteRequest) -> dict:
    target = safe_path(state, body.path)
    if target.exists():
        if target.is_dir():
            raise HTTPException(status_code=400, detail="Path is a directory")
        current = target.read_text(encoding="utf-8")
        current_hash = content_hash(current)
        # Conflict when the file changed on disk since the client last read it.
        if body.base_hash and body.base_hash != current_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "File changed on disk since it was opened",
                    "current_hash": current_hash,
                },
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    state.context.events.publish(GitChanged(paths=[body.path]))
    return {"path": body.path, "hash": content_hash(body.content)}


@router.post("/create")
def create(state: Annotated[GuiState, Depends(get_state)], body: CreateRequest) -> dict:
    target = safe_path(state, body.path)
    if target.exists():
        raise HTTPException(status_code=409, detail="Path already exists")
    if body.is_dir:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    state.context.events.publish(GitChanged(paths=[body.path]))
    return {"path": body.path, "type": "directory" if body.is_dir else "file"}


@router.post("/rename")
def rename(state: Annotated[GuiState, Depends(get_state)], body: RenameRequest) -> dict:
    source = safe_path(state, body.source, must_exist=True)
    dest = safe_path(state, body.dest)
    if dest.exists():
        raise HTTPException(status_code=409, detail="Destination already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source.replace(dest)
    state.context.events.publish(GitChanged(paths=[body.source, body.dest]))
    return {"source": body.source, "dest": body.dest}


@router.delete("/delete")
def delete(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(...),
) -> dict:
    target = safe_path(state, path, must_exist=True)
    if target == state.root:
        raise HTTPException(status_code=400, detail="Refusing to delete the project root")
    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    state.context.events.publish(GitChanged(paths=[path]))
    return {"path": path, "deleted": True}


@router.get("/search")
def search(
    state: Annotated[GuiState, Depends(get_state)],
    q: str = Query(..., min_length=1),
    regex: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    result = state.files.grep(q, limit=limit) if regex else state.files.search_text(q)
    matches = result.data.get("matches", []) if result.success else []
    return {"query": q, "matches": matches[:limit], "success": result.success}
