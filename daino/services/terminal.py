"""Interactive PTY-backed terminals for the browser IDE.

The agent's own command execution is deliberately non-interactive and sandboxed
(``daino.runtimes``); this is separate — a real login shell in a pseudo-terminal
so the user can run interactive tools from the GUI's terminal panel. Output is
streamed over a WebSocket; a bounded scrollback is kept server-side for reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import shutil
import signal
import struct
import termios
import time
from collections import deque
from pathlib import Path

from daino.utils.ids import new_id

#: Cap the server-side scrollback so a noisy process cannot grow memory without
#: bound; the live stream is unaffected and clients render their own history.
_SCROLLBACK_BYTES = 256_000

#: A shell nobody is attached to is kept this long, so a page reload finds it
#: again, and then reaped. Without this every browser tab that ever opened the
#: IDE would leave a live PTY behind until the server exits.
DEFAULT_IDLE_SECONDS = 600.0

#: Hard ceiling on concurrent shells for one project.
MAX_SESSIONS = 24


class TerminalLimitError(RuntimeError):
    """Raised when a project already has the maximum number of shells open."""


def _default_shell() -> list[str]:
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-i"] if shell.endswith(("bash", "zsh", "sh")) else [shell]


class TerminalSession:
    """One PTY child process with async read streaming and resize support."""

    def __init__(self, cwd: Path, *, command: list[str] | None = None) -> None:
        self.id = new_id("terminal")
        self.cwd = str(cwd)
        self._command = command or _default_shell()
        self._pid: int | None = None
        self._fd: int | None = None
        self._scrollback: deque[bytes] = deque()
        self._scrollback_size = 0
        self._closed = False
        #: Live client count and the moment the last one left, for reaping.
        self.clients = 0
        self.idle_since: float | None = time.monotonic()
        #: Set when the shell itself exits, so the sweeper can clear it out.
        self.finished = False

    def start(self) -> None:
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
            env = {**os.environ, "TERM": "xterm-256color"}
            os.execvpe(self._command[0], self._command, env)  # noqa: S606 - interactive PTY shell
            os._exit(127)  # pragma: no cover - exec replaces the process
        # parent
        self._pid = pid
        self._fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def scrollback(self) -> bytes:
        return b"".join(self._scrollback)

    def write(self, data: str) -> None:
        if self._fd is None or self._closed:
            return
        with contextlib.suppress(OSError):
            os.write(self._fd, data.encode("utf-8", "replace"))

    def resize(self, rows: int, cols: int) -> None:
        if self._fd is None or self._closed:
            return
        with contextlib.suppress(OSError):
            winsize = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)

    async def read(self) -> bytes | None:
        """Await the next chunk of output, or ``None`` when the shell exits."""
        if self._fd is None or self._closed:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes | None] = loop.create_future()

        def _on_readable() -> None:
            if future.done():
                return
            try:
                data = os.read(self._fd, 65536)  # type: ignore[arg-type]
            except BlockingIOError:
                return
            except OSError:
                data = b""
            loop.remove_reader(self._fd)  # type: ignore[arg-type]
            future.set_result(data or None)

        loop.add_reader(self._fd, _on_readable)
        try:
            data = await future
        finally:
            with contextlib.suppress(ValueError, OSError):
                loop.remove_reader(self._fd)  # type: ignore[arg-type]
        if data:
            self._record(data)
        else:
            self.finished = True
        return data

    def _record(self, data: bytes) -> None:
        self._scrollback.append(data)
        self._scrollback_size += len(data)
        while self._scrollback_size > _SCROLLBACK_BYTES and len(self._scrollback) > 1:
            self._scrollback_size -= len(self._scrollback.popleft())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pid is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(self._pid, signal.SIGHUP)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(self._pid, os.WNOHANG)
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


class TerminalManager:
    """Owns the live terminals for one project, keyed by id."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)
        self._sessions: dict[str, TerminalSession] = {}

    def create(self, *, command: list[str] | None = None) -> TerminalSession:
        # Sweep first: a caller at the ceiling is usually one whose old shells
        # are simply unattached, and refusing those would be wrong.
        self.prune()
        if len(self._sessions) >= MAX_SESSIONS:
            raise TerminalLimitError(
                f"This project already has {MAX_SESSIONS} terminals open; close one first"
            )
        session = TerminalSession(self.cwd, command=command)
        session.start()
        self._sessions[session.id] = session
        return session

    def attach(self, terminal_id: str) -> None:
        """Record that a client is streaming this terminal."""
        session = self._sessions.get(terminal_id)
        if session is None:
            return
        session.clients += 1
        session.idle_since = None

    def detach(self, terminal_id: str) -> None:
        """Record that a client stopped streaming; start its idle countdown."""
        session = self._sessions.get(terminal_id)
        if session is None:
            return
        session.clients = max(0, session.clients - 1)
        if session.clients == 0:
            session.idle_since = time.monotonic()

    def prune(self, *, idle_seconds: float = DEFAULT_IDLE_SECONDS) -> list[str]:
        """Close finished shells and those nobody has been attached to.

        Returns the ids that were closed, so a caller can log or report them.
        """
        now = time.monotonic()
        closed: list[str] = []
        for terminal_id, session in list(self._sessions.items()):
            expired = (
                session.clients == 0
                and session.idle_since is not None
                and now - session.idle_since >= idle_seconds
            )
            if session.finished or session.closed or expired:
                self._sessions.pop(terminal_id, None)
                session.close()
                closed.append(terminal_id)
        return closed

    def get(self, terminal_id: str) -> TerminalSession | None:
        return self._sessions.get(terminal_id)

    def list_ids(self) -> list[str]:
        return list(self._sessions)

    def close(self, terminal_id: str) -> bool:
        session = self._sessions.pop(terminal_id, None)
        if session is None:
            return False
        session.close()
        return True

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()
