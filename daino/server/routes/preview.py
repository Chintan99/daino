"""Dev-server detection and lifecycle for the Inspector's Live app view."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daino.events import PreviewStarted, PreviewStopped
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/preview", tags=["preview"])


class StartPreviewRequest(BaseModel):
    command: str
    url: str = ""


@router.get("/detect")
def detect(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    return {"commands": [asdict(item) for item in state.preview.detect()]}


@router.get("/status")
def status(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    current = state.preview.current
    if current is None:
        return {"running": False, "command": "", "url": "", "logs": []}
    return {
        "running": True,
        "command": current.command,
        "url": current.url,
        "logs": list(current.logs)[-100:],
    }


@router.post("/start")
def start(state: Annotated[GuiState, Depends(get_state)], body: StartPreviewRequest) -> dict:
    # The command is deliberately user-selected in the GUI; refuse only the
    # patterns the security policy classifies as never-approvable.
    from daino.security import PolicyEngine

    decision = PolicyEngine(state.context.settings.security).command_decision(body.command)
    if not decision.allowed:
        raise HTTPException(
            status_code=403, detail="; ".join(decision.reasons) or "Command denied by policy"
        )
    proc = state.preview.start(body.command, url=body.url)
    state.context.events.publish(PreviewStarted(url=proc.url, command=proc.command))
    _spawn_log_pump(state)
    return {"running": True, "command": proc.command, "url": proc.url}


@router.post("/stop")
def stop(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    state.preview.stop()
    state.context.events.publish(PreviewStopped())
    return {"running": False}


def _spawn_log_pump(state: GuiState) -> None:
    """Drain the preview process's stdout into its bounded log buffer."""

    async def pump() -> None:
        proc = state.preview.current
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, proc.process.stdout.readline)
            if not line:
                break
            discovered = state.preview.record_output(line.rstrip("\n"))
            if discovered:
                state.context.events.publish(PreviewStarted(url=discovered, command=proc.command))
        state.context.events.publish(PreviewStopped(reason="process exited"))

    with __import__("contextlib").suppress(RuntimeError):
        asyncio.get_running_loop().create_task(pump())
