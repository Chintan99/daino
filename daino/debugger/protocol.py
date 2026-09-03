"""A Debug Adapter Protocol client.

DAP is what every editor speaks to every debugger, and the reason to implement
it rather than to drive ``pdb`` is that one client gets Python, Node, Go, and
anything else that ships an adapter. The protocol itself is small: framed JSON
over a socket or a pipe, requests with sequence numbers, responses that carry
them back, and events that arrive whenever the debuggee does something.

Three things about DAP are load-bearing and easy to get wrong:

* **Breakpoints are set per file, not per line.** ``setBreakpoints`` replaces
  every breakpoint in the source it names, so sending one breakpoint clears the
  others in that file. The whole set for a file always goes together.
* **The adapter decides where a breakpoint actually landed.** A line with no
  executable code gets moved, or rejected. The response says which, and an
  editor that draws the marker where the user clicked rather than where the
  adapter put it is lying about where execution will stop.
* **Nothing is valid until ``configurationDone``.** The handshake is
  ``initialize`` → wait for the ``initialized`` event → send breakpoints →
  ``configurationDone``. Sending breakpoints before the event arrives is the
  most common way a debugger silently ignores them.

Everything here is transport and bookkeeping; :mod:`daino.debugger.session` owns
the lifecycle and :mod:`daino.debugger.adapters` knows how to start each one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: How long to wait for a response to one request. Generous: a debuggee stopped
#: at a breakpoint can take a moment to produce a stack of a few hundred frames.
REQUEST_TIMEOUT_SECONDS = 20.0
#: The handshake gets its own, shorter budget — an adapter that has not answered
#: `initialize` in this long is not going to.
INITIALIZE_TIMEOUT_SECONDS = 15.0

EventHandler = Callable[[str, dict[str, Any]], None]


class DebugError(RuntimeError):
    """Raised when an adapter cannot be reached, or refuses a request."""


@dataclass
class _Pending:
    future: asyncio.Future[dict[str, Any]]
    command: str


@dataclass
class DebugCapabilities:
    """What this adapter supports, from its ``initialize`` response.

    Read rather than assumed: asking an adapter for something it does not
    support is an error it reports as a failed request, which surfaces to the
    user as a broken button instead of an absent one.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return bool(self.raw.get(name))

    @property
    def configuration_done(self) -> bool:
        return self.has("supportsConfigurationDoneRequest")

    @property
    def conditional_breakpoints(self) -> bool:
        return self.has("supportsConditionalBreakpoints")

    @property
    def terminate(self) -> bool:
        return self.has("supportsTerminateRequest")

    @property
    def set_variable(self) -> bool:
        return self.has("supportsSetVariable")


class DebugAdapterClient:
    """Speaks DAP over a pair of streams.

    Transport-agnostic on purpose: debugpy is easiest to drive over a TCP
    socket, Node's inspector adapters over stdio, and the protocol above them is
    identical. The caller supplies the reader and writer.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_event: EventHandler | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.on_event = on_event
        self.capabilities = DebugCapabilities()
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._reader_task: asyncio.Task[None] | None = None
        #: Set when the adapter has asked for its configuration. Breakpoints
        #: sent before this are ignored by most adapters, silently.
        self.initialized = asyncio.Event()
        #: Set when the debuggee has exited or the adapter has gone.
        self.terminated = asyncio.Event()
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(DebugError("The debug adapter stopped."))
        self._pending.clear()
        with contextlib.suppress(Exception):
            self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
        self.terminated.set()

    # --------------------------------------------------------------- requests

    def send(
        self, command: str, arguments: dict[str, Any] | None = None
    ) -> asyncio.Future[dict[str, Any]]:
        """Send a request and hand back its future, without waiting.

        Needed for exactly one thing, and it is not an optimisation:
        **``launch`` and ``attach`` do not answer until configuration is
        done.** The real DAP order is

            initialize → (initialized event) → setBreakpoints →
            configurationDone → *then* the launch/attach response

        so a client that awaits the attach response before sending
        ``configurationDone`` waits forever, and debugpy in particular will sit
        there indefinitely. Every other request can be awaited normally.
        """
        self._seq += 1
        seq = self._seq
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[seq] = _Pending(future, command)
        self._write(
            {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}
        )
        return future

    async def request(
        self, command: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request and wait for its response.

        A response with ``success: false`` becomes an exception carrying the
        adapter's own message, because the adapter's wording ("Breakpoint on
        line 4 has no code") is nearly always better than anything this layer
        could invent.
        """
        future = self.send(command, arguments)
        return await self.settle(command, future, timeout=timeout)

    async def settle(
        self,
        command: str,
        future: asyncio.Future[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for a request sent earlier with :meth:`send`."""
        try:
            return await asyncio.wait_for(future, timeout or REQUEST_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise DebugError(f"The debug adapter did not answer {command}.") from exc

    def _write(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        try:
            self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        except (OSError, RuntimeError) as exc:
            raise DebugError(f"The debug adapter is not reachable: {exc}") from exc

    # ---------------------------------------------------------------- reading

    async def _read_loop(self) -> None:
        try:
            while True:
                length = 0
                while True:
                    line = await self.reader.readline()
                    if not line:
                        self.terminated.set()
                        return
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        break
                    name, _, value = text.partition(":")
                    if name.strip().casefold() == "content-length":
                        with contextlib.suppress(ValueError):
                            length = int(value.strip())
                if length <= 0:
                    continue
                payload = await self.reader.readexactly(length)
                try:
                    message = json.loads(payload.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            self.terminated.set()
        except Exception:  # noqa: BLE001 - a dead reader must not kill the app
            self.terminated.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "response":
            pending = self._pending.pop(int(message.get("request_seq", 0)), None)
            if pending is None or pending.future.done():
                return
            if message.get("success"):
                pending.future.set_result(message.get("body") or {})
                return
            pending.future.set_exception(
                DebugError(str(message.get("message") or f"{pending.command} was refused."))
            )
            return
        if kind == "event":
            event = str(message.get("event", ""))
            body = message.get("body") or {}
            if event == "initialized":
                self.initialized.set()
            elif event in {"terminated", "exited"}:
                self.terminated.set()
            if self.on_event is not None:
                with contextlib.suppress(Exception):
                    self.on_event(event, body)
            return
        if kind == "request":
            # Reverse requests: `runInTerminal` is the common one, and refusing
            # it politely is better than leaving the adapter waiting forever.
            with contextlib.suppress(DebugError):
                self._write(
                    {
                        "seq": 0,
                        "type": "response",
                        "request_seq": message.get("seq", 0),
                        "command": message.get("command", ""),
                        "success": False,
                        "message": "Daino runs the debuggee itself.",
                    }
                )

    # ------------------------------------------------------------- handshake

    async def initialize(self, adapter_id: str) -> DebugCapabilities:
        body = await self.request(
            "initialize",
            {
                "clientID": "daino",
                "clientName": "Daino",
                "adapterID": adapter_id,
                "locale": "en",
                # One-based on both axes, matching the editor and every error
                # message anyone reads. The alternative is converting at three
                # different layers and getting one of them wrong.
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                "supportsVariableType": True,
                "supportsVariablePaging": False,
                "supportsRunInTerminalRequest": False,
                "supportsProgressReporting": False,
            },
            timeout=INITIALIZE_TIMEOUT_SECONDS,
        )
        self.capabilities = DebugCapabilities(raw=body)
        return self.capabilities

    async def configuration_done(self) -> None:
        """Close the configuration phase, if this adapter has one.

        Guarded on the capability: adapters that do not advertise it treat the
        request as unknown and answer with an error, which would surface as a
        failed launch for no reason.
        """
        if self.capabilities.configuration_done:
            await self.request("configurationDone")
