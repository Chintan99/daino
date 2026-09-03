"""Dev-server detection and lifecycle for the Inspector's Live app view."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daino.events import PreviewStarted, PreviewStopped
from daino.security import PolicyEngine
from daino.security.policy import Permission
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/preview", tags=["preview"])


class StartPreviewRequest(BaseModel):
    command: str
    url: str = ""
    #: The user has seen why this command needs approval and said yes. Sent by
    #: the Start button's confirmation step, never defaulted on.
    confirm: bool = False


def _policy(state: GuiState) -> PolicyEngine:
    return PolicyEngine(state.context.settings.security)


def _approval(state: GuiState, command: str) -> tuple[bool, list[str], bool]:
    """Classify one preview command: (refused, reasons, needs confirmation).

    Three outcomes, not two. The route used to collapse the middle one into a
    refusal, which is what stopped Docker Compose projects starting at all:
    ``docker compose up`` mutates the host's Docker state, so the policy asks
    before it runs — and an endpoint with no way to ask read that as "denied".
    """
    decision = _policy(state).command_decision(command)
    if decision.allowed:
        return False, [], False
    if decision.permission is Permission.DELETE_RESOURCE:
        # A destructive pattern is never a dev server, whoever confirms it.
        return True, list(decision.reasons), False
    if decision.requires_approval:
        return False, list(decision.reasons), True
    return True, list(decision.reasons), False


@router.get("/detect")
def detect(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Candidate commands, each carrying whether starting it needs a yes.

    Reported here as well as enforced at start, so the button can say
    "Start (needs approval)" instead of failing on the click.
    """
    commands = []
    for item in state.preview.detect():
        refused, reasons, needs_approval = _approval(state, item.command)
        commands.append(
            {
                **asdict(item),
                "refused": refused,
                "requires_approval": needs_approval,
                "approval_reasons": reasons,
            }
        )
    return {"commands": commands}


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
    # The command is deliberately user-selected in the GUI, so selection plus an
    # explicit confirmation *is* the approval. What is refused outright is only
    # what no confirmation should buy: a destructive pattern, a command the
    # project has denied, shell syntax that cannot run without a shell.
    refused, reasons, needs_approval = _approval(state, body.command)
    if refused:
        raise HTTPException(
            status_code=403, detail="; ".join(reasons) or "Command denied by policy"
        )
    if needs_approval and not body.confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "requires_approval": True,
                "command": body.command,
                "reasons": reasons,
            },
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
