"""Filesystem browsing and editing with optimistic-concurrency conflict checks."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.config import paths
from daino.events import GitChanged
from daino.repository.search import (
    SearchQuery,
    apply_replacement,
)
from daino.repository.search import search as repo_search
from daino.server.deps import content_hash, get_state, language_for, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/files", tags=["files"])

_IGNORED = {
    ".git",
    *paths.STATE_DIR_NAMES,
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
}
_MAX_EDITABLE_BYTES = 2_000_000

#: Where a file dropped on the chat box lands. Inside the state directory rather
#: than the working tree: an attachment is conversation material, not a change
#: to the repository, and it must not turn up as an untracked file in the diff
#: the user is about to review.
_ATTACHMENT_DIR = "attachments"
#: Attachments are conversation context, not asset hosting.
_MAX_ATTACHMENT_BYTES = 8_000_000


class WriteRequest(BaseModel):
    path: str
    content: str
    #: sha256 of the content the client last read; guards against clobbering an
    #: out-of-band change. Omit/empty for a brand-new file.
    base_hash: str = ""


class AttachRequest(BaseModel):
    """One file dropped, pasted, or picked in the chat composer."""

    name: str = Field(min_length=1, max_length=255)
    #: Base64 so an image or any other binary survives the JSON round trip.
    content_base64: str


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


@router.post("/attach")
def attach_file(state: Annotated[GuiState, Depends(get_state)], body: AttachRequest) -> dict:
    """Store an attachment and return the path the agent can act on.

    The agent reads files by path, so an attachment becomes a real file it can
    open rather than bytes smuggled through the prompt. Names are sanitised and
    the target is always inside the state directory's attachment folder, so a
    crafted name cannot write elsewhere.
    """
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", body.name.strip()).strip("-.") or "attachment"
    try:
        payload = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Not valid base64: {exc}") from exc
    if len(payload) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment is larger than {_MAX_ATTACHMENT_BYTES // 1_000_000} MB",
        )

    directory = paths.state_dir(state.root, create=True) / _ATTACHMENT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name
    # Never overwrite: two screenshots pasted in a row are two attachments.
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for index in range(1, 1000):
            candidate = directory / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                target = candidate
                break
    target.write_bytes(payload)
    return {
        "path": str(target.relative_to(state.root)),
        "name": target.name,
        "bytes": len(payload),
    }


def _query(
    q: str,
    regex: bool,
    case_sensitive: bool,
    whole_word: bool,
    include: str,
    exclude: str,
    limit: int,
) -> SearchQuery:
    """Build a query from the flat form the search box posts.

    Include/exclude arrive comma-separated because that is what a single text
    input can carry, and blanks are dropped so a trailing comma is harmless
    rather than a filter that matches nothing.
    """
    return SearchQuery(
        query=q,
        regex=regex,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        include=tuple(item.strip() for item in include.split(",") if item.strip()),
        exclude=tuple(item.strip() for item in exclude.split(",") if item.strip()),
        limit=limit,
    )


@router.get("/search")
def search(
    state: Annotated[GuiState, Depends(get_state)],
    q: str = Query(..., min_length=1),
    regex: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    whole_word: bool = Query(default=False),
    include: str = Query(default=""),
    exclude: str = Query(default=""),
    limit: int = Query(default=500, ge=1, le=5000),
    replace: str | None = Query(default=None),
) -> dict:
    """Find text across the repository, with filters.

    ``replace`` makes this a preview rather than a search: every match then
    carries the line it *would* become, and nothing is written. Applying is a
    separate POST, because a tree-wide replacement is one of the few editor
    operations that can quietly ruin a working copy.
    """
    query = _query(q, regex, case_sensitive, whole_word, include, exclude, limit)
    result = repo_search(state.root, query, replacement=replace)
    return {
        "query": q,
        "success": not result.error,
        "error": result.error,
        "files": result.files,
        "truncated": result.truncated,
        "skipped": result.skipped,
        "matches": [
            {
                "path": match.path,
                "line": match.line,
                "column": match.column,
                "length": match.length,
                "text": match.text,
                "replacement": match.replacement,
            }
            for match in result.matches
        ],
    }


class ReplaceRequest(BaseModel):
    """Apply a previewed replacement."""

    query: str = Field(min_length=1)
    replacement: str
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    include: str = ""
    exclude: str = ""
    #: The files the user actually ticked. Empty means everything the filters
    #: match, which is only what someone means if they said so.
    paths: list[str] = Field(default_factory=list)


@router.post("/replace")
def replace_in_files(state: Annotated[GuiState, Depends(get_state)], body: ReplaceRequest) -> dict:
    """Write a replacement across the repository.

    Recomputed from the query rather than applied from the preview's text: a
    file edited between preview and apply must not be written from a stale
    snapshot.
    """
    for path in body.paths:
        safe_path(state, path)
    query = _query(
        body.query,
        body.regex,
        body.case_sensitive,
        body.whole_word,
        body.include,
        body.exclude,
        5_000,
    )
    summary = apply_replacement(state.root, query, body.replacement, only_paths=body.paths or None)
    if summary.errors and not summary.files:
        raise HTTPException(status_code=400, detail="; ".join(summary.errors))
    if summary.files:
        state.context.events.publish(GitChanged(paths=summary.files))
    return {
        "files": summary.files,
        "replacements": summary.replacements,
        "errors": summary.errors,
    }
