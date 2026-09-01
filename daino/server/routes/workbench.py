"""Workspace CRUD: goals, artifacts, tasks, uploads, sources, and history.

Mirrors :mod:`daino.server.routes.design`: one service backs both this route and
the agent's tools, so a document the user edits here and one the agent rewrites
are the same file on disk.

Artifact paths arrive from the browser and are therefore untrusted. Every one of
them goes through :class:`~daino.workbench.service.WorkbenchService`, which
resolves and checks containment before touching the disk — the route never joins
a path itself.

Unlike :mod:`~daino.server.routes.design`, which wraps each service call to map
its error, a :class:`WorkbenchError` becomes a 404 through one application-level
exception handler registered in :func:`daino.server.app.create_app`. With this
many routes, a per-call wrapper is noise, and a decorator would erase the
signatures FastAPI reads to build each request model.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.server.deps import get_state
from daino.server.state import GuiState
from daino.workbench.models import TaskStatus
from daino.workbench.service import WorkbenchError

router = APIRouter(prefix="/api/workspaces", tags=["workspace"])

#: Matches the attachment ceiling in ``routes/files.py``: one upload path, one
#: limit, so a file that can be attached to a chat can be added to a workspace.
MAX_UPLOAD_BYTES = 8_000_000


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = ""
    kind: str = "general"
    #: Repository-relative folder. Empty picks ``workspace/<slug>``.
    folder: str = ""


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    goal: str | None = None
    kind: str | None = None
    status: str | None = None


class WriteArtifactRequest(BaseModel):
    path: str = Field(min_length=1)
    content: str
    author: str = "user"


class SetTasksRequest(BaseModel):
    tasks: list[str]


class AddTaskRequest(BaseModel):
    content: str = Field(min_length=1)


class UpdateTaskRequest(BaseModel):
    content: str | None = None
    status: TaskStatus | None = None
    notes: str | None = None
    artifact_path: str | None = None


class ReorderTasksRequest(BaseModel):
    task_ids: list[str]


class UploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_base64: str


class AttachSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


# ------------------------------------------------------------------ workspaces


@router.get("")
def list_workspaces(
    state: Annotated[GuiState, Depends(get_state)],
    include_archived: bool = Query(default=False),
) -> dict[str, Any]:
    return {
        "workspaces": [
            item.model_dump(mode="json")
            for item in state.workbench.list_workspaces(include_archived=include_archived)
        ]
    }


@router.post("")
def create_workspace(
    state: Annotated[GuiState, Depends(get_state)], body: CreateWorkspaceRequest
) -> dict[str, Any]:
    workspace = state.workbench.create(
        body.name,
        goal=body.goal,
        kind=body.kind,
        folder=body.folder,
    )
    return workspace.model_dump(mode="json")


@router.get("/templates")
def list_templates(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """The work types a new workspace can start from."""
    return {
        "templates": [item.model_dump(mode="json") for item in state.workbench.templates.list()]
    }


@router.get("/{workspace_id}")
def get_workspace(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    return state.workbench.get(workspace_id).model_dump(mode="json")


@router.patch("/{workspace_id}")
def update_workspace(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: UpdateWorkspaceRequest,
) -> dict[str, Any]:
    workspace = state.workbench.update(
        workspace_id,
        name=body.name,
        goal=body.goal,
        kind=body.kind,
        status=body.status,
    )
    return workspace.model_dump(mode="json")


@router.delete("/{workspace_id}")
def delete_workspace(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    remove_files: bool = Query(default=False),
) -> dict[str, Any]:
    """Remove a workspace, and its folder only when asked explicitly.

    Two decisions rather than one: forgetting a workspace is reversible, and
    deleting written work is not.
    """
    state.workbench.delete(workspace_id, remove_files=remove_files)
    return {"deleted": workspace_id, "files_removed": remove_files}


@router.post("/{workspace_id}/session")
def attach_session(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: AttachSessionRequest,
) -> dict[str, Any]:
    """Point a conversation at this workspace so its history accumulates."""
    state.workbench.attach_session(workspace_id, body.session_id)
    return {"workspace_id": workspace_id, "session_id": body.session_id}


# ------------------------------------------------------------------- artifacts


@router.get("/{workspace_id}/artifacts")
def list_artifacts(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    workspace = state.workbench.get(workspace_id)
    return {
        "artifacts": [item.model_dump(mode="json") for item in workspace.artifacts],
        "uploads": [item.model_dump(mode="json") for item in workspace.uploads],
    }


@router.get("/{workspace_id}/artifact")
def read_artifact(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    path: str = Query(min_length=1),
) -> dict[str, Any]:
    """Read one artifact.

    The path is a query parameter rather than a path segment so a nested
    document ("notes/january.md") needs no encoding gymnastics, and so a
    traversal attempt reaches the service's containment check as data.
    """
    return state.workbench.artifact(workspace_id, path).model_dump(mode="json")


@router.put("/{workspace_id}/artifact")
def write_artifact(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: WriteArtifactRequest,
) -> dict[str, Any]:
    artifact = state.workbench.write_artifact(
        workspace_id, body.path, body.content, author=body.author
    )
    return artifact.model_dump(mode="json")


@router.delete("/{workspace_id}/artifact")
def delete_artifact(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    path: str = Query(min_length=1),
) -> dict[str, Any]:
    state.workbench.delete_artifact(workspace_id, path)
    return {"deleted": path}


# --------------------------------------------------------------------- history


@router.get("/{workspace_id}/revisions")
def list_revisions(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    path: str = Query(min_length=1),
) -> dict[str, Any]:
    return {
        "path": path,
        "revisions": [
            item.model_dump(mode="json") for item in state.workbench.revisions(workspace_id, path)
        ],
    }


@router.get("/{workspace_id}/revision")
def read_revision(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    path: str = Query(min_length=1),
    version: int = Query(ge=1),
) -> dict[str, Any]:
    return {
        "path": path,
        "version": version,
        "content": state.workbench.revision_content(workspace_id, path, version),
    }


@router.post("/{workspace_id}/revision/restore")
def restore_revision(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    path: str = Query(min_length=1),
    version: int = Query(ge=1),
) -> dict[str, Any]:
    artifact = state.workbench.restore_revision(workspace_id, path, version)
    return artifact.model_dump(mode="json")


# ----------------------------------------------------------------------- tasks


@router.get("/{workspace_id}/tasks")
def list_tasks(state: Annotated[GuiState, Depends(get_state)], workspace_id: str) -> dict[str, Any]:
    workspace = state.workbench.get(workspace_id)
    return {"tasks": [item.model_dump(mode="json") for item in workspace.tasks]}


@router.put("/{workspace_id}/tasks")
def set_tasks(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: SetTasksRequest,
) -> dict[str, Any]:
    tasks = state.workbench.set_tasks(workspace_id, body.tasks)
    return {"tasks": [item.model_dump(mode="json") for item in tasks]}


@router.post("/{workspace_id}/tasks")
def add_task(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: AddTaskRequest,
) -> dict[str, Any]:
    return state.workbench.add_task(workspace_id, body.content).model_dump(mode="json")


@router.post("/{workspace_id}/tasks/reorder")
def reorder_tasks(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: ReorderTasksRequest,
) -> dict[str, Any]:
    tasks = state.workbench.reorder_tasks(workspace_id, body.task_ids)
    return {"tasks": [item.model_dump(mode="json") for item in tasks]}


@router.patch("/{workspace_id}/tasks/{task_id}")
def update_task(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    task_id: str,
    body: UpdateTaskRequest,
) -> dict[str, Any]:
    task = state.workbench.update_task(
        workspace_id,
        task_id,
        content=body.content,
        status=body.status,
        notes=body.notes,
        artifact_path=body.artifact_path,
    )
    return task.model_dump(mode="json")


@router.delete("/{workspace_id}/tasks/{task_id}")
def delete_task(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str, task_id: str
) -> dict[str, Any]:
    state.workbench.delete_task(workspace_id, task_id)
    return {"deleted": task_id}


# --------------------------------------------------------------------- sources


@router.get("/{workspace_id}/sources")
def list_sources(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    workspace = state.workbench.get(workspace_id)
    return {"sources": [item.model_dump(mode="json") for item in workspace.sources]}


# --------------------------------------------------------------------- uploads


@router.post("/{workspace_id}/uploads")
async def upload(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: UploadRequest,
) -> dict[str, Any]:
    """Store a file in the workspace and extract its text.

    Extraction runs in a thread: a large PDF takes seconds to parse, and the
    event loop is also serving the agent's own turn.
    """
    try:
        payload = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Not valid base64: {exc}") from exc
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Uploads are limited to {MAX_UPLOAD_BYTES // 1_000_000} MB",
        )
    try:
        artifact = await asyncio.to_thread(
            state.workbench.save_upload, workspace_id, body.name, payload
        )
    except WorkbenchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return artifact.model_dump(mode="json")
