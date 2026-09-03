"""Run configurations and tasks: the commands this project already declares.

Discovered from `package.json`, Makefiles, justfiles, compose files and
`pyproject.toml` rather than from a Daino-specific launch format — a project's
npm scripts *are* its run configurations, and asking anyone to restate them
would be asking them to keep two lists in step.

These endpoints only ever read and return commands. Running one means opening a
terminal and sending it there, which the GUI does explicitly, because these
strings come out of files in the repository and the point at which one becomes a
process should be visible.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from daino.repository import runconfigs
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class UserTask(BaseModel):
    """One command the user added themselves."""

    #: Reusing a discovered id overrides that command, which is the point.
    id: str = ""
    label: str = Field(min_length=1)
    command: str = Field(min_length=1)
    cwd: str = ""
    detail: str = ""
    kind: str = "other"


class SaveTasksRequest(BaseModel):
    tasks: list[UserTask] = Field(default_factory=list)


def _describe(config: runconfigs.RunConfig) -> dict[str, Any]:
    return {
        "id": config.id,
        "label": config.label,
        "command": config.command,
        "source": config.source,
        "cwd": config.cwd,
        "detail": config.detail,
        "kind": config.kind,
    }


@router.get("")
def list_tasks(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """Every runnable command, grouped by what it is for."""
    found = runconfigs.discover(state.root)
    return {
        "tasks": [_describe(item) for item in found],
        # The user's own file, so the editor can offer to open it.
        "tasks_file": f".daino/{runconfigs.TASKS_FILE}",
    }


@router.get("/{task_id:path}")
def get_task(state: Annotated[GuiState, Depends(get_state)], task_id: str) -> dict[str, Any]:
    """One command, resolved — what a Run button would actually execute."""
    found = runconfigs.by_id(state.root, task_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Unknown task {task_id}")
    return _describe(found)


@router.put("")
def save_tasks(
    state: Annotated[GuiState, Depends(get_state)], body: SaveTasksRequest
) -> dict[str, Any]:
    """Replace ``.daino/tasks.json`` with these entries."""
    path = runconfigs.save_user_tasks(
        state.root,
        [item.model_dump(mode="json") for item in body.tasks],
    )
    return {
        "saved": str(path.relative_to(state.root)),
        "tasks": [_describe(item) for item in runconfigs.discover(state.root)],
    }
