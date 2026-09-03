"""One debug session: launch, breakpoints, stepping, stack, and variables.

The state here is what a debugger UI actually renders, kept on the server rather
than in the browser for the same reason the QA run is: a debug session outlives
the tab that started it. Reloading the page while stopped at a breakpoint should
show the same frame, not an empty panel.

The one piece of real subtlety is **breakpoint verification**. The user clicks a
line; the adapter decides where execution can actually stop, and may move the
breakpoint or reject it. Both the requested line and the verified line are kept,
because drawing the marker where the click happened while the debugger stops
three lines lower is the kind of small lie that makes people distrust the whole
tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from daino.debugger import adapters
from daino.debugger.protocol import DebugAdapterClient, DebugError
from daino.utils.ids import new_id

#: How long to wait for an adapter to accept a connection after being started.
CONNECT_TIMEOUT_SECONDS = 20.0
#: How often to retry the connection while the adapter is still binding.
CONNECT_INTERVAL_SECONDS = 0.1
#: Variables fetched per scope. A dict with 100k keys must not become 100k rows.
MAX_VARIABLES = 500

SessionState = Literal["starting", "running", "stopped", "terminated", "failed"]

Notify = Callable[[], None]


@dataclass
class Breakpoint:
    """One breakpoint, as requested and as the adapter resolved it."""

    #: Repository-relative, which is what the editor's gutter speaks.
    path: str
    #: Where the user clicked.
    line: int
    condition: str = ""
    #: Whether the adapter accepted it. False means execution will not stop.
    verified: bool = False
    #: Where the adapter actually put it. Differs when the requested line holds
    #: no executable code, and the difference must be visible.
    actual_line: int = 0
    #: Why it was rejected, when it was.
    message: str = ""

    @property
    def moved(self) -> bool:
        return bool(self.actual_line and self.actual_line != self.line)


@dataclass
class StackFrame:
    id: int
    name: str
    path: str
    line: int
    column: int = 1


@dataclass
class Variable:
    name: str
    value: str
    type: str = ""
    #: Non-zero when this value can be expanded — an object, list, or dict.
    variables_reference: int = 0


@dataclass
class Scope:
    name: str
    variables_reference: int
    #: Set for scopes an adapter warns are slow to read, e.g. globals.
    expensive: bool = False


@dataclass
class DebugSession:
    """Everything a debugger panel renders."""

    id: str
    adapter: str
    state: SessionState = "starting"
    #: What is being debugged, for the panel's header.
    program: str = ""
    #: Why the debuggee stopped: "breakpoint", "step", "exception", "entry".
    stop_reason: str = ""
    #: The thread that stopped. DAP is multi-threaded; the UI shows one.
    thread_id: int = 0
    frames: list[StackFrame] = field(default_factory=list)
    #: Console output from the debuggee, newest last.
    output: list[str] = field(default_factory=list)
    #: Set when the session failed to start, or the adapter died.
    error: str = ""
    #: The debuggee's exit code, once it has one.
    exit_code: int | None = None


class DebugManager:
    """Owns the one debug session a project may have running.

    One at a time, deliberately. Two debuggees sharing a working tree, a
    database, and usually a port produce a state nobody can reason about, and
    the panel has room for one call stack.
    """

    def __init__(self, root: Path, *, on_change: Notify | None = None) -> None:
        self.root = Path(root).resolve()
        self.on_change = on_change
        self.session: DebugSession | None = None
        self.client: DebugAdapterClient | None = None
        self._process: asyncio.subprocess.Process | None = None
        #: Breakpoints survive the session: they are the user's, not the run's.
        #: Keyed by repository-relative path, because DAP replaces a whole
        #: file's set at a time.
        self.breakpoints: dict[str, list[Breakpoint]] = {}
        self._pump: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ breakpoints

    def toggle_breakpoint(self, path: str, line: int) -> list[Breakpoint]:
        """Add or remove a breakpoint, returning the file's whole set."""
        current = self.breakpoints.setdefault(path, [])
        existing = next((item for item in current if item.line == line), None)
        if existing is not None:
            current.remove(existing)
        else:
            current.append(Breakpoint(path=path, line=line))
        current.sort(key=lambda item: item.line)
        if not current:
            self.breakpoints.pop(path, None)
        return list(current)

    def set_condition(self, path: str, line: int, condition: str) -> list[Breakpoint]:
        for item in self.breakpoints.get(path, []):
            if item.line == line:
                item.condition = condition.strip()
        return list(self.breakpoints.get(path, []))

    def clear_breakpoints(self, path: str = "") -> None:
        if path:
            self.breakpoints.pop(path, None)
        else:
            self.breakpoints.clear()

    async def sync_breakpoints(self, path: str = "") -> None:
        """Push breakpoints to a running adapter.

        Per file, because ``setBreakpoints`` *replaces* everything in the source
        it names — sending one breakpoint clears the rest of that file. Files
        whose breakpoints were all removed still have to be sent, as an empty
        list, or the adapter keeps the ones it already has.
        """
        client = self.client
        if client is None:
            return
        targets = [path] if path else list(self.breakpoints)
        for relative in targets:
            wanted = self.breakpoints.get(relative, [])
            try:
                body = await client.request(
                    "setBreakpoints",
                    {
                        "source": {"path": str(self.root / relative), "name": Path(relative).name},
                        "breakpoints": [self._describe(item, client) for item in wanted],
                    },
                )
            except DebugError as exc:
                for item in wanted:
                    item.verified = False
                    item.message = str(exc)
                continue
            # The adapter answers in the same order it was asked.
            for item, resolved in zip(wanted, body.get("breakpoints") or [], strict=False):
                item.verified = bool(resolved.get("verified"))
                item.actual_line = int(resolved.get("line") or 0)
                item.message = str(resolved.get("message") or "")
        self._changed()

    @staticmethod
    def _describe(item: Breakpoint, client: DebugAdapterClient) -> dict[str, Any]:
        """One breakpoint in DAP's shape.

        The condition is only sent when the adapter says it understands
        conditions; sending one to an adapter that does not would have it
        rejected, and a breakpoint silently dropped for a feature nobody asked
        about is the worst kind of failure.
        """
        described: dict[str, Any] = {"line": item.line}
        if item.condition and client.capabilities.conditional_breakpoints:
            described["condition"] = item.condition
        return described

    # ---------------------------------------------------------------- launch

    async def launch(
        self,
        *,
        program: str = "",
        module: str = "",
        args: list[str] | None = None,
        stop_on_entry: bool = False,
    ) -> DebugSession:
        """Start a debuggee under an adapter and connect to it."""
        if self.running:
            raise DebugError("A debug session is already running.")
        if not program and not module:
            raise DebugError("Nothing to debug: name a file or a module.")
        language = adapters.language_of(program) if program else "python"
        adapter = adapters.for_language(language)
        if adapter is None:
            raise DebugError(f"No debug adapter is available for {language or 'this file'}.")
        if adapter is not adapters.PYTHON:
            raise DebugError(
                f"{adapter.label} is recognised but not yet driven by Daino. "
                "Python debugging is available now."
            )

        target = program or module
        session = DebugSession(
            id=new_id("debug"),
            adapter=adapter.id,
            state="starting",
            program=target,
        )
        self.session = session
        self._changed()

        port = adapters.free_port()
        argv = (
            adapters.python_launch_argv(self.root, port, str(self.root / program), args or [])
            if program
            else adapters.python_module_argv(self.root, port, module, args or [])
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.root),
                env={**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"},
            )
        except (OSError, ValueError) as exc:
            session.state = "failed"
            session.error = f"Could not start the debuggee: {exc}"
            self._changed()
            raise DebugError(session.error) from exc

        self._pump = asyncio.create_task(self._pump_output())
        try:
            reader, writer = await self._connect(port)
        except DebugError:
            session.state = "failed"
            session.error = (
                "The debug adapter never accepted a connection. Check that "
                "debugpy is installed in this project's interpreter."
            )
            await self.stop()
            self._changed()
            raise

        client = DebugAdapterClient(reader, writer, on_event=self._on_event)
        client.start()
        self.client = client
        try:
            await client.initialize(adapter.id)
            # `attach` rather than `launch`: debugpy is already running the
            # program and waiting for us, which is what --wait-for-client did.
            #
            # Sent without awaiting, because the attach response does not come
            # until configuration is finished. The order DAP actually requires
            # is: attach → (initialized event) → setBreakpoints →
            # configurationDone → attach response. Awaiting attach here is a
            # deadlock, and a silent one: debugpy simply never answers.
            attached = client.send(
                "attach",
                {
                    "justMyCode": True,
                    "pathMappings": [{"localRoot": str(self.root), "remoteRoot": str(self.root)}],
                },
            )
            # Breakpoints only stick after the adapter asks for configuration.
            # Sending them earlier is the classic way they are silently ignored.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(client.initialized.wait(), 10.0)
            await self.sync_breakpoints()
            await client.configuration_done()
            await client.settle("attach", attached)
            if stop_on_entry:
                await client.request("pause", {"threadId": 1})
        except DebugError as exc:
            session.state = "failed"
            session.error = str(exc)
            await self.stop()
            self._changed()
            raise
        session.state = "running"
        self._changed()
        return session

    async def _connect(self, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Retry until the adapter has bound its port, or give up."""
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise DebugError("The debuggee exited before the debugger attached.")
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(CONNECT_INTERVAL_SECONDS)
        raise DebugError("Timed out connecting to the debug adapter.")

    # ------------------------------------------------------------- execution

    @property
    def running(self) -> bool:
        return self.session is not None and self.session.state in {
            "starting",
            "running",
            "stopped",
        }

    async def resume(self) -> None:
        await self._control("continue", {"threadId": self._thread()})

    async def pause(self) -> None:
        await self._control("pause", {"threadId": self._thread()})

    async def step_over(self) -> None:
        await self._control("next", {"threadId": self._thread()})

    async def step_into(self) -> None:
        await self._control("stepIn", {"threadId": self._thread()})

    async def step_out(self) -> None:
        await self._control("stepOut", {"threadId": self._thread()})

    async def _control(self, command: str, arguments: dict[str, Any]) -> None:
        client = self.client
        if client is None or self.session is None:
            raise DebugError("No debug session is running.")
        await client.request(command, arguments)
        if command != "pause":
            # The debuggee is moving again; the stack it had is gone. Clearing
            # it here rather than waiting for the next `stopped` event stops the
            # panel showing a frame that is no longer current.
            self.session.state = "running"
            self.session.frames = []
            self._changed()

    async def stop(self) -> None:
        """End the session and the debuggee with it."""
        client = self.client
        if client is not None:
            with contextlib.suppress(Exception):
                if client.capabilities.terminate:
                    await client.request("terminate", {}, timeout=5.0)
                else:
                    await client.request("disconnect", {"terminateDebuggee": True}, timeout=5.0)
            await client.close()
        self.client = None
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5.0)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
        if self.session is not None and self.session.state not in {"failed"}:
            self.session.state = "terminated"
        # Breakpoints outlive the session: they are the user's, not the run's.
        for items in self.breakpoints.values():
            for item in items:
                item.verified = False
                item.actual_line = 0
        self._changed()

    # ------------------------------------------------------------- inspection

    async def stack(self) -> list[StackFrame]:
        """The current call stack, innermost first."""
        client = self.client
        if client is None or self.session is None or self.session.state != "stopped":
            return []
        body = await client.request(
            "stackTrace", {"threadId": self._thread(), "startFrame": 0, "levels": 100}
        )
        frames = [
            StackFrame(
                id=int(frame.get("id", 0)),
                name=str(frame.get("name", "")),
                path=self._relative(str((frame.get("source") or {}).get("path", ""))),
                line=int(frame.get("line", 0)),
                column=int(frame.get("column", 1)),
            )
            for frame in body.get("stackFrames") or []
        ]
        self.session.frames = frames
        self._changed()
        return frames

    async def scopes(self, frame_id: int) -> list[Scope]:
        client = self.client
        if client is None:
            return []
        body = await client.request("scopes", {"frameId": frame_id})
        return [
            Scope(
                name=str(scope.get("name", "")),
                variables_reference=int(scope.get("variablesReference", 0)),
                expensive=bool(scope.get("expensive")),
            )
            for scope in body.get("scopes") or []
        ]

    async def variables(self, reference: int) -> list[Variable]:
        client = self.client
        if client is None or reference <= 0:
            return []
        body = await client.request("variables", {"variablesReference": reference})
        return [
            Variable(
                name=str(item.get("name", "")),
                value=str(item.get("value", "")),
                type=str(item.get("type") or ""),
                variables_reference=int(item.get("variablesReference", 0)),
            )
            for item in (body.get("variables") or [])[:MAX_VARIABLES]
        ]

    async def evaluate(self, expression: str, frame_id: int = 0) -> dict[str, Any]:
        """Evaluate an expression in a frame's context.

        Scoped to a frame rather than globally: "what is `total` here" is the
        question people actually ask at a breakpoint, and answering it from
        module scope would give a different and misleading answer.
        """
        client = self.client
        if client is None:
            raise DebugError("No debug session is running.")
        body = await client.request(
            "evaluate",
            {
                "expression": expression,
                "context": "repl",
                **({"frameId": frame_id} if frame_id else {}),
            },
        )
        return {
            "result": str(body.get("result", "")),
            "type": str(body.get("type") or ""),
            "variables_reference": int(body.get("variablesReference", 0)),
        }

    # ------------------------------------------------------------------ events

    def _on_event(self, event: str, body: dict[str, Any]) -> None:
        session = self.session
        if session is None:
            return
        if event == "stopped":
            session.state = "stopped"
            session.stop_reason = str(body.get("reason", ""))
            session.thread_id = int(body.get("threadId") or session.thread_id or 1)
            # The stack is fetched on demand rather than here: this runs on the
            # reader task, and awaiting a request from it would deadlock.
            session.frames = []
        elif event == "continued":
            session.state = "running"
            session.frames = []
        elif event == "output":
            text = str(body.get("output", ""))
            if text:
                session.output.append(text)
                del session.output[:-2_000]
        elif event == "exited":
            session.exit_code = int(body.get("exitCode") or 0)
        elif event == "terminated":
            session.state = "terminated"
            session.frames = []
        elif event == "breakpoint":
            self._apply_breakpoint_event(body)
        self._changed()

    def _apply_breakpoint_event(self, body: dict[str, Any]) -> None:
        """An adapter can move or verify a breakpoint after the fact."""
        resolved = body.get("breakpoint") or {}
        source = str((resolved.get("source") or {}).get("path", ""))
        if not source:
            return
        relative = self._relative(source)
        line = int(resolved.get("line") or 0)
        for item in self.breakpoints.get(relative, []):
            if item.line == line or item.actual_line == line:
                item.verified = bool(resolved.get("verified"))
                item.actual_line = line
                item.message = str(resolved.get("message") or "")

    async def _pump_output(self) -> None:
        """Forward the debuggee's stdout into the session's console.

        debugpy relays most output as DAP `output` events, but anything written
        before the adapter attached — an import-time error, most usefully —
        only appears on the pipe.
        """
        process = self._process
        if process is None or process.stdout is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                if self.session is not None:
                    self.session.output.append(line.decode("utf-8", "replace"))
                    del self.session.output[:-2_000]
                    self._changed()

    # ------------------------------------------------------------------ pieces

    def _thread(self) -> int:
        return (self.session.thread_id if self.session else 0) or 1

    def _relative(self, absolute: str) -> str:
        if not absolute:
            return ""
        try:
            return Path(absolute).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return absolute

    def _changed(self) -> None:
        if self.on_change is not None:
            with contextlib.suppress(Exception):
                self.on_change()
