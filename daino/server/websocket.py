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

#: Policy violation, per RFC 6455 — the client is told why and not retried.
_CLOSE_POLICY = 1008

router = APIRouter()


class _Connection:
    """One session WebSocket: serialized sends + pending approval futures."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._send_lock = asyncio.Lock()
        self._approvals: dict[str, asyncio.Future[tuple[bool, bool]]] = {}
        self._approval_seq = 0
        #: Set when the browser goes away. The turn keeps running, so its sends
        #: have to become no-ops rather than exceptions.
        self.closed = False

    async def send(self, message: dict) -> None:
        """Best-effort delivery: a gone client must not fail a running turn."""
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.websocket.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                # RuntimeError is Starlette's "close message already sent".
                self.closed = True

    def abandon(self) -> None:
        """The client left: stop sending, and stop waiting for its answers.

        Pending approvals are resolved as *denied* rather than left hanging. An
        unanswerable approval would hold the turn — and the project's turn lock —
        open forever, which is a worse outcome than the agent being told no and
        reporting that it could not run the command.
        """
        self.closed = True
        for future in self._approvals.values():
            if not future.done():
                future.set_result((False, False))
        self._approvals.clear()

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


async def _origin_refused(websocket: WebSocket) -> bool:
    """Close the handshake unless it comes from this server's own page.

    WebSockets are exempt from CORS, so without this any site the user visits
    could drive the agent or type into a shell on their machine.
    """
    policy = websocket.app.state.origins
    reason = policy.rejection(websocket.headers.get("origin"), websocket.headers.get("host"))
    if reason is None:
        return False
    await websocket.close(code=_CLOSE_POLICY, reason=reason)
    return True


def _turn_summary(outcome: object) -> str:
    """One line about how a chat turn ended, for the notification body."""
    answer = str(getattr(outcome, "answer", "") or getattr(outcome, "summary", "") or "")
    if getattr(outcome, "changed", False):
        verified = getattr(outcome, "verified", None)
        state = "verified" if verified else "unverified" if verified is None else "failing checks"
        return f"Changes written ({state}). {answer}".strip()
    return answer or "Turn finished"


async def _steer_active_run(
    state: GuiState, session_id: str, text: str, connection: _Connection
) -> bool:
    """Route a message typed during a run into that run. True when it was.

    Recorded in the conversation first so the panel shows what was said, then
    handed to the executor, which folds it into the plan at the next step
    boundary rather than interrupting the step in flight.
    """
    run = state.runs.active_for_session(session_id)
    if run is None:
        return False
    state.missions.add_message(session_id, kind="user", role="user", content=text)
    try:
        state.runs.steer(run.id, text)
    except DainoError as exc:
        await connection.send({"type": "error", "message": str(exc)})
        return True
    state.missions.add_message(
        session_id,
        kind="status",
        role="",
        content="Plan updated from your instruction — it takes effect at the next step.",
    )
    await connection.send({"type": "run_steered", "session_id": session_id, "run_id": run.id})
    return True


def _event_message(event: MissionEvent) -> dict:
    payload = event.payload()
    payload["kind"] = event.kind
    return {"type": "event", "event": payload}


@router.websocket("/ws/session/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    state: GuiState = websocket.app.state.gui
    if await _origin_refused(websocket):
        return
    await websocket.accept()

    # Resolve sentinels so a client can connect before it knows a concrete id.
    if session_id in ("latest", "new"):
        session_id = (
            state.missions.create_session()
            if session_id == "new"
            else state.missions.latest_session()
        )
    # A reconnecting client (a page refresh, most often) has to learn that work
    # is still in flight, or it shows an idle agent while the server is building.
    await websocket.send_json(
        {
            "type": "session",
            "session_id": session_id,
            "turn_running": state.turn_busy,
        }
    )

    connection = _Connection(websocket)
    queue = state.context.events.open_stream()

    async def pump_events() -> None:
        while True:
            event = await queue.get()
            await connection.send(_event_message(event))

    pump = asyncio.create_task(pump_events())

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        return await connection.request_approval(command, reason)

    async def run_turn(text: str, profile: str) -> None:
        # One turn at a time for the whole process: a second browser tab shares
        # this runtime, and two concurrent turns would interleave tool calls and
        # file edits against the same working tree.
        try:
            await state.turn_lock.acquire()
        except asyncio.CancelledError:
            raise
        try:
            # Holds a sleep inhibitor for the turn and raises an OS notification
            # when it ends — the same helper the terminal client uses, so a user
            # who walked away is told either way.
            async with state.missions.attention.turn("Browser turn") as attention:
                try:
                    outcome = await state.missions.chat(
                        text, session_id, profile_override=profile, approve=approve
                    )
                    attention.completed(_turn_summary(outcome))
                    await connection.send({"type": "turn_complete", "session_id": session_id})
                except asyncio.CancelledError:
                    # The user pressed Stop. `CancelledError` is a BaseException,
                    # so without this it slipped past the handlers below and the
                    # browser was never told the turn ended — the runner spun
                    # forever. Tell it, then let the cancellation propagate. The
                    # notice is shielded so it is not cancelled with this task.
                    attention.failed("Stopped by user")
                    notice = asyncio.ensure_future(
                        connection.send({"type": "turn_stopped", "session_id": session_id})
                    )
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(notice)
                    raise
                except DainoError as exc:
                    attention.failed(str(exc))
                    await connection.send({"type": "error", "message": str(exc)})
                except Exception as exc:  # noqa: BLE001 - report, never crash the socket
                    attention.failed(f"Turn failed: {exc}")
                    await connection.send({"type": "error", "message": f"Turn failed: {exc}"})
        finally:
            state.turn_lock.release()

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "user_message":
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                # A workspace run holds the turn lock for each of its steps, so
                # without this the composer would refuse every message for the
                # duration of a plan. Typing while a plan runs is not a second
                # turn — it is direction for the one already going.
                if await _steer_active_run(state, session_id, text, connection):
                    continue
                # Claimed synchronously, before the task exists. Testing
                # `turn_lock.locked()` here left a window one tick wide in which
                # a second sender — or a design route — saw an idle project and
                # started its own agent against the same working tree.
                if not state.claim_turn():
                    await connection.send(
                        {
                            "type": "error",
                            "message": (
                                "Another D[Ai]NO turn is already running for this "
                                "project — wait for it to finish, or stop it there."
                            ),
                        }
                    )
                    continue
                profile = message.get("profile") or ""
                # Kept on the shared state, not this connection: a turn survives
                # a refresh, so the *next* connection has to be able to stop it.
                turn = asyncio.create_task(run_turn(text, profile))
                # On the task rather than in `run_turn`'s `finally`, so a turn
                # cancelled before its first step still frees the slot.
                turn.add_done_callback(lambda _: state.release_turn())
                state.active_turn = turn
            elif kind == "approval_resolve":
                connection.resolve_approval(
                    message.get("id", ""),
                    bool(message.get("approved")),
                    bool(message.get("remember")),
                )
            elif kind == "cancel":
                task = state.active_turn
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
            elif kind == "ping":
                await connection.send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        # Deliberately *not* cancelling the turn. The work is server-side, so a
        # refreshed or closed tab used to kill a running mission silently:
        # CancelledError is not an Exception, so nothing reported it and the
        # mission was left orphaned mid-flight. Only an explicit `cancel`
        # message stops a turn now; a reconnecting client picks it back up.
        connection.abandon()
        state.context.events.close_stream(queue)


@router.websocket("/ws/terminal/{terminal_id}")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    state: GuiState = websocket.app.state.gui
    if await _origin_refused(websocket):
        return
    await websocket.accept()
    session = state.terminals.get(terminal_id)
    if session is None:
        await websocket.send_json({"type": "error", "message": "Unknown terminal"})
        await websocket.close()
        return
    state.terminals.attach(terminal_id)

    # Replay bounded scrollback so a reconnecting client sees recent output.
    scrollback = session.scrollback
    if scrollback:
        await websocket.send_json({"type": "output", "data": scrollback.decode("utf-8", "replace")})

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
        state.terminals.detach(terminal_id)
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
