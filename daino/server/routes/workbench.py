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

from daino.application.workspace_run_service import RunError
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
    #: Repository-relative folder. Empty picks ``.daino/workspaces/<slug>``.
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


class StartRunRequest(BaseModel):
    """Execute this workspace's plan."""

    #: What the run is for. Empty falls back to the workspace's own goal.
    goal: str = ""
    #: Model profile to pin for every turn of the run, as the composer does.
    profile: str = ""
    #: Skill to work by. Empty lets Daino choose one from the goal.
    skill: str = ""


class SteerRequest(BaseModel):
    instruction: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1)
    approved: bool


class LinkRequest(BaseModel):
    """Record that one document was made from another."""

    source_path: str = Field(min_length=1)
    target_path: str = ""
    relation: str = "derived_from"
    source_kind: str = "artifact"
    target_kind: str = "artifact"
    title: str = ""


class DeliverableRequest(BaseModel):
    """Render a workspace document into a file people can open."""

    path: str = Field(min_length=1)
    format: str = Field(min_length=2, max_length=8)
    title: str = ""


class DecideChangeRequest(BaseModel):
    """Keep or undo a change — one artifact, or the whole set."""

    accepted: bool
    #: Empty decides every still-pending artifact in the set.
    path: str = ""


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


# ------------------------------------------------------------------- runs
#
# A run is long-lived, so these routes start it and report on it rather than
# holding a request open for its duration — the same shape QA and change review
# already use. Progress arrives over the session WebSocket as
# ``WorkspaceRunUpdated`` events; this is what a reconnecting client reads to
# catch up.


def _run_payload(state: GuiState, run: object | None) -> dict[str, Any]:
    return {"run": run.model_dump(mode="json") if run is not None else None}


@router.get("/{workspace_id}/run")
def current_run(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    """The active run, or the most recent one, so a reopened tab can report."""
    return _run_payload(state, state.runs.latest(workspace_id))


@router.get("/{workspace_id}/runs")
def run_history(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    return {
        "runs": [item.model_dump(mode="json") for item in state.runs.runs.history_for(workspace_id)]
    }


@router.post("/{workspace_id}/run")
async def start_run(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: StartRunRequest,
) -> dict[str, Any]:
    try:
        run = await state.runs.start(
            workspace_id, goal=body.goal, profile=body.profile, skill=body.skill
        )
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_payload(state, run)


@router.post("/runs/{run_id}/pause")
def pause_run(state: Annotated[GuiState, Depends(get_state)], run_id: str) -> dict[str, Any]:
    return _run_payload(state, _guard(lambda: state.runs.pause(run_id)))


@router.post("/runs/{run_id}/resume")
async def resume_run(state: Annotated[GuiState, Depends(get_state)], run_id: str) -> dict[str, Any]:
    try:
        run = await state.runs.resume(run_id)
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_payload(state, run)


@router.post("/runs/{run_id}/stop")
def stop_run(state: Annotated[GuiState, Depends(get_state)], run_id: str) -> dict[str, Any]:
    return _run_payload(state, _guard(lambda: state.runs.stop(run_id)))


@router.post("/runs/{run_id}/steer")
def steer_run(
    state: Annotated[GuiState, Depends(get_state)], run_id: str, body: SteerRequest
) -> dict[str, Any]:
    """Take new direction mid-run without discarding finished work."""
    return _run_payload(state, _guard(lambda: state.runs.steer(run_id, body.instruction)))


@router.post("/runs/{run_id}/approval")
def resolve_run_approval(
    state: Annotated[GuiState, Depends(get_state)], run_id: str, body: ApprovalRequest
) -> dict[str, Any]:
    return _run_payload(
        state,
        _guard(lambda: state.runs.resolve_approval(run_id, body.approval_id, body.approved)),
    )


@router.post("/runs/{run_id}/tasks/{task_id}/retry")
async def retry_task(
    state: Annotated[GuiState, Depends(get_state)], run_id: str, task_id: str
) -> dict[str, Any]:
    """Reopen a failed step and continue the run from it."""
    run = _guard(lambda: state.runs.get(run_id))
    state.workbench.update_task(run.workspace_id, task_id, status="pending", error="")
    state.runs.runs.add_step(run_id, "note", "Retrying the failed step.", task_id=task_id)
    try:
        resumed = await state.runs.resume(run_id)
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_payload(state, resumed)


@router.post("/runs/{run_id}/tasks/{task_id}/skip")
async def skip_task(
    state: Annotated[GuiState, Depends(get_state)], run_id: str, task_id: str
) -> dict[str, Any]:
    """Leave a step undone and carry on with the rest of the plan.

    Recorded as failed rather than completed on purpose: the step did not
    happen, and a progress count that says otherwise is the one number a reader
    trusts.
    """
    run = _guard(lambda: state.runs.get(run_id))
    state.workbench.update_task(
        run.workspace_id, task_id, status="failed", error="Skipped by the user."
    )
    state.runs.runs.add_step(run_id, "task_skipped", "Skipped by the user.", task_id=task_id)
    try:
        resumed = await state.runs.resume(run_id)
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_payload(state, resumed)


@router.get("/meta/skills")
def list_skills(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """Every skill available here, built-ins plus the project's own."""
    return {"skills": [item.model_dump(mode="json") for item in state.runs.skills.list()]}


def _guard(call: Any) -> Any:
    try:
        return call()
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------- change sets
#
# The revision history is untouched and still the source of truth. These routes
# read the index that says which revisions were one act, and express every
# decision in terms of that history: rejecting restores the artifact's previous
# revision through the same path the History panel uses.


@router.get("/{workspace_id}/changes")
def list_changes(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    run_id: str = Query(default=""),
) -> dict[str, Any]:
    changes = state.runs.changes.list_for(workspace_id, run_id=run_id)
    return {"changes": [item.model_dump(mode="json") for item in changes]}


@router.get("/{workspace_id}/changes/{change_set_id}")
def read_change(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str, change_set_id: str
) -> dict[str, Any]:
    return state.runs.changes.get(change_set_id).model_dump(mode="json")


@router.get("/{workspace_id}/changes/{change_set_id}/diff")
def read_change_diff(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    change_set_id: str,
    path: str = Query(...),
) -> dict[str, Any]:
    """What this change did to one artifact, both sides read from history."""
    return state.runs.changes.diff(change_set_id, path).model_dump(mode="json")


@router.post("/{workspace_id}/changes/{change_set_id}/decide")
def decide_change(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    change_set_id: str,
    body: DecideChangeRequest,
) -> dict[str, Any]:
    changes = state.runs.changes
    result = (
        changes.decide(change_set_id, body.path, accepted=body.accepted)
        if body.path
        else changes.decide_all(change_set_id, accepted=body.accepted)
    )
    return result.model_dump(mode="json")


# --------------------------------------------------- relationships and output


@router.get("/{workspace_id}/links")
def list_links(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str
) -> dict[str, Any]:
    """How this workspace's outputs relate, and which may have gone stale."""
    return {
        "links": [item.model_dump(mode="json") for item in state.links.links_for(workspace_id)],
        "stale": [item.model_dump(mode="json") for item in state.links.stale(workspace_id)],
    }


@router.post("/{workspace_id}/links")
def create_link(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str, body: LinkRequest
) -> dict[str, Any]:
    link = state.links.link(
        workspace_id,
        source_path=body.source_path,
        target_path=body.target_path,
        relation=body.relation,
        source_kind=body.source_kind,
        target_kind=body.target_kind,
        title=body.title,
    )
    return link.model_dump(mode="json")


@router.delete("/{workspace_id}/links/{link_id}")
def delete_link(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str, link_id: str
) -> dict[str, Any]:
    state.links.unlink(workspace_id, link_id)
    return {"deleted": link_id}


@router.post("/{workspace_id}/links/{link_id}/acknowledge")
def acknowledge_link(
    state: Annotated[GuiState, Depends(get_state)], workspace_id: str, link_id: str
) -> dict[str, Any]:
    """Dismiss a staleness warning without changing the document.

    Durable on purpose: a warning that returns after being dismissed teaches
    people to ignore warnings.
    """
    state.links.acknowledge(workspace_id, link_id)
    return {"acknowledged": link_id}


@router.post("/{workspace_id}/deliverable")
async def create_deliverable(
    state: Annotated[GuiState, Depends(get_state)],
    workspace_id: str,
    body: DeliverableRequest,
) -> dict[str, Any]:
    """Render a document into docx, xlsx, pptx or pdf, beside the source.

    In a thread: a large deck takes a moment to build, and the event loop is
    also serving the agent's own turn.
    """
    artifact = await asyncio.to_thread(
        state.workbench.save_deliverable,
        workspace_id,
        body.path,
        body.format.strip().lstrip(".").casefold(),
        title=body.title,
    )
    return artifact.model_dump(mode="json")
