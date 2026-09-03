"""Language intelligence: diagnostics, navigation, symbols, and rename.

Backed by whatever language servers the machine has (see
:mod:`daino.repository.lsp`). Every endpoint here is careful to distinguish
three outcomes that are easy to confuse and expensive to confuse:

* the server looked and found nothing — a clean file,
* the server cannot serve this language — no analyser exists here,
* the server is not installed — nothing looked, and here is how to fix it.

The GUI renders all three differently, which is why they are separate fields
rather than an empty list and a shrug.

Positions on the wire are one-based, matching the editor and every compiler
error anyone has ever read. The conversion to LSP's zero-based coordinates
happens once, at this boundary.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.repository.lsp import (
    LSPError,
    available_servers,
    language_id_for,
)
from daino.server.deps import get_state, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/lsp", tags=["lsp"])

#: How long a diagnostics request waits for the server's first publish. Long
#: enough for a cold server to finish its initial index, short enough that the
#: editor does not appear to hang on a file nobody will get an answer for.
DIAGNOSTICS_TIMEOUT_SECONDS = 8.0


class DocumentRequest(BaseModel):
    path: str = Field(min_length=1)
    #: The editor's buffer. Sent so diagnostics describe what the user is
    #: looking at rather than what was last written to disk.
    text: str | None = None


class PositionRequest(BaseModel):
    path: str = Field(min_length=1)
    #: One-based, as shown in the editor's gutter.
    line: int = Field(ge=1)
    column: int = Field(ge=1, default=1)


class RenameRequest(PositionRequest):
    new_name: str = Field(min_length=1)


def _unsupported(path: str) -> dict[str, Any]:
    """The honest answer for a file no language server can analyse."""
    return {
        "path": path,
        "supported": False,
        "available": False,
        "diagnostics": [],
        "detail": "No language server covers this file type.",
    }


@router.get("/servers")
def servers(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """Which language servers exist, which are installed, and how to get them.

    The Problems panel shows this so "no diagnostics" is never mistaken for "no
    problems" — a missing analyser is a gap in evidence, and the user is the
    only one who can close it.
    """
    return {
        "servers": available_servers(state.root),
        "running": state.lsp.pool.running(),
    }


@router.post("/diagnostics")
async def diagnostics(
    state: Annotated[GuiState, Depends(get_state)], body: DocumentRequest
) -> dict[str, Any]:
    """Problems in one document, from its language server."""
    target = safe_path(state, body.path)
    if not language_id_for(target):
        return _unsupported(body.path)
    try:
        found = await state.lsp.diagnostics(target, body.text, timeout=DIAGNOSTICS_TIMEOUT_SECONDS)
    except LSPError as exc:
        # Not an HTTP error: the request was fine, the analyser simply is not
        # here. The panel needs to say so rather than show a failed request.
        return {
            "path": body.path,
            "supported": True,
            "available": False,
            "diagnostics": [],
            "detail": str(exc),
        }
    return {
        "path": body.path,
        "supported": True,
        "available": True,
        "diagnostics": [{**item, "path": body.path} for item in found],
        "detail": "",
    }


@router.post("/close")
def close_document(
    state: Annotated[GuiState, Depends(get_state)], body: DocumentRequest
) -> dict[str, Any]:
    """Tell the server a file is no longer open, so it stops tracking it."""
    target = safe_path(state, body.path)
    state.lsp.close_document(target)
    return {"closed": body.path}


@router.post("/definition")
async def definition(
    state: Annotated[GuiState, Depends(get_state)], body: PositionRequest
) -> dict[str, Any]:
    return await _navigate(state, body, "definition")


@router.post("/references")
async def references(
    state: Annotated[GuiState, Depends(get_state)], body: PositionRequest
) -> dict[str, Any]:
    return await _navigate(state, body, "references")


@router.post("/implementations")
async def implementations(
    state: Annotated[GuiState, Depends(get_state)], body: PositionRequest
) -> dict[str, Any]:
    return await _navigate(state, body, "implementations")


@router.post("/hover")
async def hover(
    state: Annotated[GuiState, Depends(get_state)], body: PositionRequest
) -> dict[str, Any]:
    target = safe_path(state, body.path)
    if not language_id_for(target):
        return {"available": False, "markdown": "", "detail": _unsupported(body.path)["detail"]}
    try:
        text = await state.lsp.hover(target, body.line - 1, body.column - 1)
    except LSPError as exc:
        return {"available": False, "markdown": "", "detail": str(exc)}
    return {"available": True, "markdown": text, "detail": ""}


@router.get("/symbols")
async def document_symbols(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(min_length=1),
) -> dict[str, Any]:
    """The outline of one file."""
    target = safe_path(state, path)
    if not language_id_for(target):
        return {"available": False, "symbols": [], "detail": _unsupported(path)["detail"]}
    try:
        found = await state.lsp.symbols(target)
    except LSPError as exc:
        return {"available": False, "symbols": [], "detail": str(exc)}
    return {
        "available": True,
        "symbols": [item.model_dump(mode="json") for item in found],
        "detail": "",
    }


@router.get("/workspace-symbols")
async def workspace_symbols(
    state: Annotated[GuiState, Depends(get_state)],
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Symbols across the project, for jump-to-symbol.

    Falls back to the repository index when no language server can answer, so
    the search box works in a fresh checkout with nothing installed — with less
    precision, which the ``source`` field states rather than hides.
    """
    try:
        found = await state.lsp.workspace_symbols(query)
        source = "language-server"
    except LSPError:
        # Nothing answered — fall back to the index and say that is what this
        # is. Labelling index hits as language-server hits would overstate their
        # precision, and these results are text-derived, not semantic.
        found = state.repository.find_symbol(query) if query else []
        source = "index"
    return {
        "symbols": [item.model_dump(mode="json") for item in found[:limit]],
        "source": source,
        "query": query,
    }


@router.post("/rename")
async def rename(
    state: Annotated[GuiState, Depends(get_state)], body: RenameRequest
) -> dict[str, Any]:
    """Every edit a rename implies — returned, never applied.

    A cross-file refactor is exactly the kind of change that should be seen
    before it happens, so this hands back the edit list and the GUI shows it.
    Applying is a separate, explicit call.
    """
    target = safe_path(state, body.path)
    if not language_id_for(target):
        return {"available": False, "edits": {}, "detail": _unsupported(body.path)["detail"]}
    try:
        edits = await state.lsp.rename_edits(target, body.line - 1, body.column - 1, body.new_name)
    except LSPError as exc:
        return {"available": False, "edits": {}, "detail": str(exc)}
    return {
        "available": True,
        "edits": edits,
        "files": len(edits),
        "count": sum(len(items) for items in edits.values()),
        "detail": "",
    }


@router.post("/rename/apply")
async def apply_rename(
    state: Annotated[GuiState, Depends(get_state)],
    edits: Annotated[dict[str, list[dict[str, Any]]], Body(embed=True)],
) -> dict[str, Any]:
    """Write a rename's edits to disk, after the user has seen them.

    Applied back-to-front within each file so an earlier edit cannot shift the
    coordinates of a later one — the single detail that makes multi-edit
    application correct without recomputing positions.
    """
    written: list[str] = []
    for relative, items in sorted(edits.items()):
        target = safe_path(state, relative, must_exist=True)
        try:
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"{relative}: {exc}") from exc
        ordered = sorted(
            items,
            key=lambda item: (int(item["start_line"]), int(item["start_column"])),
            reverse=True,
        )
        for item in ordered:
            lines = _apply_edit(lines, item)
        target.write_text("".join(lines), encoding="utf-8")
        written.append(relative)
    if written:
        await asyncio.sleep(0)  # let the file watcher see the writes as one batch
    return {"written": written}


def _apply_edit(lines: list[str], edit: dict[str, Any]) -> list[str]:
    """Replace one span. Coordinates are one-based and end-exclusive."""
    start_line = int(edit["start_line"]) - 1
    end_line = int(edit["end_line"]) - 1
    start_column = int(edit["start_column"]) - 1
    end_column = int(edit["end_column"]) - 1
    if start_line < 0 or start_line >= len(lines):
        return lines
    end_line = min(end_line, len(lines) - 1)
    head = lines[start_line][:start_column]
    tail = lines[end_line][end_column:]
    return [*lines[:start_line], head + str(edit.get("text", "")) + tail, *lines[end_line + 1 :]]


async def _navigate(state: GuiState, body: PositionRequest, kind: str) -> dict[str, Any]:
    target = safe_path(state, body.path)
    if not language_id_for(target):
        return {"available": False, "locations": [], "detail": _unsupported(body.path)["detail"]}
    method = {
        "definition": state.lsp.definition,
        "references": state.lsp.references,
        "implementations": state.lsp.implementations,
    }[kind]
    try:
        locations = await method(target, body.line - 1, body.column - 1)
    except LSPError as exc:
        # The index knows textual occurrences even when no server is installed.
        # Less precise, and said so: a rename must never be driven from this.
        if kind == "references":
            fallback = state.repository.find_references_at(body.path, body.line)
            if fallback:
                return {
                    "available": False,
                    "locations": fallback,
                    "detail": f"{exc} Showing text matches from the index instead.",
                    "source": "index",
                }
        return {"available": False, "locations": [], "detail": str(exc)}
    return {"available": True, "locations": locations, "detail": "", "source": "language-server"}
