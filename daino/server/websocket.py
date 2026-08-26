"""WebSocket endpoints: agent session streaming and interactive terminals.

The session socket bridges the existing :class:`~daino.events.EventBus` to the
browser and supplies a socket-backed :data:`~daino.tools.commands.ApprovalCallback`,
so the GUI drives the *same* agent turn as the TUI — streamed events in, command
approvals round-tripped, no duplicated agent logic.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from daino.events import MissionEvent
from daino.exceptions import DainoError
from daino.server.state import GuiState

router = APIRouter()


class _Connection:
    """One session WebSocket: serialized sends + pending approval futures."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._send_lock = asyncio.Lock()
        self._approvals: dict[str, asyncio.Future[tuple[bool, bool]]] = {}
        self._approval_seq = 0

    async def send(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def request_approval(self, command: str, reason: str) -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, bool]] = loop.create_future()
        self._approval_seq += 1
        request_id = f"approval-{self._approval_seq}"
        self._approvals[request_id] = future
        await self.send(
            {
                "type": "approval_request",
                "id": request_id,
                "command": command,
                "reason": reason,
            }
        )
        try:
            return await future
        finally:
            self._approvals.pop(request_id, None)

    def resolve_approval(self, request_id: str, approved: bool, remember: bool) -> None:
        future = self._approvals.get(request_id)
        if future is not None and not future.done():
            future.set_result((approved, remember))


def _event_message(event: MissionEvent) -> dict:
    payload = event.payload()
    payload["kind"] = event.kind
    return {"type": "event", "event": payload}


@router.websocket("/ws/session/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    state: GuiState = websocket.app.state.gui
    await websocket.accept()

    # Resolve sentinels so a client can connect before it knows a concrete id.
    if session_id in ("latest", "new"):
        session_id = (
            state.missions.create_session()
            if session_id == "new"
            else state.missions.latest_session()
        )
    await websocket.send_json({"type": "session", "session_id": session_id})

    connection = _Connection(websocket)
    queue = state.context.events.open_stream()
    turn_task: asyncio.Task | None = None

    async def pump_events() -> None:
        while True:
            event = await queue.get()
            await connection.send(_event_message(event))

    pump = asyncio.create_task(pump_events())

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        return await connection.request_approval(command, reason)

    async def run_turn(text: str, profile: str) -> None:
        try:
            await state.missions.chat(text, session_id, profile_override=profile, approve=approve)
            await connection.send({"type": "turn_complete", "session_id": session_id})
        except DainoError as exc:
            await connection.send({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report, never crash the socket
            await connection.send({"type": "error", "message": f"Turn failed: {exc}"})

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "user_message":
                if turn_task is not None and not turn_task.done():
                    await connection.send(
                        {"type": "error", "message": "A turn is already running"}
                    )
                    continue
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                profile = message.get("profile") or ""
                turn_task = asyncio.create_task(run_turn(text, profile))
            elif kind == "approval_resolve":
                connection.resolve_approval(
                    message.get("id", ""),
                    bool(message.get("approved")),
                    bool(message.get("remember")),
                )
            elif kind == "cancel":
                if turn_task is not None and not turn_task.done():
                    turn_task.cancel()
            elif kind == "ping":
                await connection.send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        state.context.events.close_stream(queue)


@router.websocket("/ws/terminal/{terminal_id}")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    state: GuiState = websocket.app.state.gui
    await websocket.accept()
    session = state.terminals.get(terminal_id)
    if session is None:
        await websocket.send_json({"type": "error", "message": "Unknown terminal"})
        await websocket.close()
        return

    # Replay bounded scrollback so a reconnecting client sees recent output.
    scrollback = session.scrollback
    if scrollback:
        await websocket.send_json(
            {"type": "output", "data": scrollback.decode("utf-8", "replace")}
        )

    async def pump_output() -> None:
        while True:
            data = await session.read()
            if data is None:
                await websocket.send_json({"type": "exit"})
                return
            await websocket.send_json({"type": "output", "data": data.decode("utf-8", "replace")})

    pump = asyncio.create_task(pump_output())
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "input":
                session.write(message.get("data", ""))
            elif kind == "resize":
                session.resize(int(message.get("rows", 24)), int(message.get("cols", 80)))
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
