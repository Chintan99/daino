"""Debugging: breakpoints, launch, stepping, stack, variables, evaluation.

Breakpoints live here rather than in the browser because they are the user's
rather than the session's: they survive a page reload, a restart of the
debuggee, and switching files. The rest of the state is server-side for the same
reason a QA run is — reloading the tab while stopped at a breakpoint should show
the same frame, not an empty panel.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.debugger import DebugError, adapters
from daino.server.deps import get_state, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/debug", tags=["debug"])


class BreakpointRequest(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)


class ConditionRequest(BreakpointRequest):
    #: Empty clears the condition, which is how a conditional breakpoint
    #: becomes an ordinary one again.
    condition: str = ""


class LaunchRequest(BaseModel):
    #: A repository-relative file. Mutually exclusive with `module`.
    program: str = ""
    #: A dotted module, for projects whose entry point is `-m something`.
    module: str = ""
    args: list[str] = Field(default_factory=list)
    stop_on_entry: bool = False


class EvaluateRequest(BaseModel):
    expression: str = Field(min_length=1)
    #: Which frame to evaluate in. "What is `total` here" is the question
    #: people ask at a breakpoint, and module scope gives a different answer.
    frame_id: int = 0


def _describe_breakpoints(state: GuiState) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path,
            "line": item.line,
            "condition": item.condition,
            "verified": item.verified,
            "actual_line": item.actual_line,
            "moved": item.moved,
            "message": item.message,
        }
        for items in state.debugger.breakpoints.values()
        for item in items
    ]


def _payload(state: GuiState) -> dict[str, Any]:
    session = state.debugger.session
    return {
        "running": state.debugger.running,
        "breakpoints": _describe_breakpoints(state),
        "session": (
            {
                "id": session.id,
                "adapter": session.adapter,
                "state": session.state,
                "program": session.program,
                "stop_reason": session.stop_reason,
                "thread_id": session.thread_id,
                "error": session.error,
                "exit_code": session.exit_code,
                "output": session.output[-400:],
                "frames": [
                    {
                        "id": frame.id,
                        "name": frame.name,
                        "path": frame.path,
                        "line": frame.line,
                        "column": frame.column,
                    }
                    for frame in session.frames
                ],
            }
            if session is not None
            else None
        ),
    }


@router.get("/adapters")
def list_adapters(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """Which debug adapters exist, which are installed, and how to get them.

    So a language with no debugger says *why* — a missing adapter is a gap the
    user can close, and it must not look the same as a debugger that found
    nothing.
    """
    return {"adapters": adapters.available(state.root)}


@router.get("/state")
def read_state(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """The session and the breakpoints, for a panel that has just mounted."""
    return _payload(state)


@router.post("/breakpoints/toggle")
async def toggle_breakpoint(
    state: Annotated[GuiState, Depends(get_state)], body: BreakpointRequest
) -> dict[str, Any]:
    """Add or remove a breakpoint, and push the file's set if a session is live."""
    safe_path(state, body.path)
    state.debugger.toggle_breakpoint(body.path, body.line)
    if state.debugger.running:
        # The whole file goes together: setBreakpoints replaces the source's
        # entire set, so sending one would clear the others.
        await state.debugger.sync_breakpoints(body.path)
    return _payload(state)


@router.post("/breakpoints/condition")
async def set_condition(
    state: Annotated[GuiState, Depends(get_state)], body: ConditionRequest
) -> dict[str, Any]:
    safe_path(state, body.path)
    state.debugger.set_condition(body.path, body.line, body.condition)
    if state.debugger.running:
        await state.debugger.sync_breakpoints(body.path)
    return _payload(state)


@router.delete("/breakpoints")
async def clear_breakpoints(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(default=""),
) -> dict[str, Any]:
    if path:
        safe_path(state, path)
    state.debugger.clear_breakpoints(path)
    if state.debugger.running:
        await state.debugger.sync_breakpoints(path)
    return _payload(state)


@router.post("/launch")
async def launch(
    state: Annotated[GuiState, Depends(get_state)], body: LaunchRequest
) -> dict[str, Any]:
    if body.program:
        safe_path(state, body.program, must_exist=True)
    try:
        await state.debugger.launch(
            program=body.program,
            module=body.module,
            args=body.args,
            stop_on_entry=body.stop_on_entry,
        )
    except DebugError as exc:
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _payload(state)


@router.post("/{command}")
async def control(state: Annotated[GuiState, Depends(get_state)], command: str) -> dict[str, Any]:
    """Continue, pause, step, or stop.

    One endpoint because they are one thing from the user's point of view — the
    row of buttons on a debug toolbar — and separate handlers would be five
    copies of the same error mapping.
    """
    actions = {
        "continue": state.debugger.resume,
        "pause": state.debugger.pause,
        "step-over": state.debugger.step_over,
        "step-into": state.debugger.step_into,
        "step-out": state.debugger.step_out,
        "stop": state.debugger.stop,
    }
    action = actions.get(command)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Unknown debug command {command}")
    try:
        await action()
    except DebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _payload(state)


@router.get("/stack")
async def stack(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """The current call stack, fetched on demand.

    Not pushed from the ``stopped`` event: that arrives on the reader task, and
    issuing a request from there would deadlock against the response it is
    waiting to read.
    """
    try:
        await state.debugger.stack()
    except DebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _payload(state)


@router.get("/scopes")
async def scopes(
    state: Annotated[GuiState, Depends(get_state)],
    frame_id: int = Query(ge=0),
) -> dict[str, Any]:
    try:
        found = await state.debugger.scopes(frame_id)
    except DebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "scopes": [
            {
                "name": item.name,
                "variables_reference": item.variables_reference,
                "expensive": item.expensive,
            }
            for item in found
        ]
    }


@router.get("/variables")
async def variables(
    state: Annotated[GuiState, Depends(get_state)],
    reference: int = Query(ge=1),
) -> dict[str, Any]:
    try:
        found = await state.debugger.variables(reference)
    except DebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "variables": [
            {
                "name": item.name,
                "value": item.value,
                "type": item.type,
                "variables_reference": item.variables_reference,
            }
            for item in found
        ]
    }


@router.post("/evaluate")
async def evaluate(
    state: Annotated[GuiState, Depends(get_state)], body: EvaluateRequest
) -> dict[str, Any]:
    try:
        return await state.debugger.evaluate(body.expression, body.frame_id)
    except DebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
