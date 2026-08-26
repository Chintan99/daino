"""Turn a native crash into something a person can act on.

A segmentation fault kills the interpreter outright: no traceback, no log line,
nothing in the transcript — the terminal simply returns. That is the worst
possible failure for a TUI, because the user has no way to tell anyone what
happened beyond "it crashed".

``faulthandler`` installs signal handlers for SIGSEGV, SIGBUS, SIGFPE, SIGILL
and SIGABRT that dump the Python stack of every thread from inside the signal
handler, before the process dies. Pointing it at a file means the next crash
leaves a stack naming the exact line, whether or not anyone was watching.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from daino.config import paths

#: Kept open for the life of the process: the handler writes from inside a
#: signal handler, where opening a file is not safe.
_handle: TextIO | None = None


#: Undo everything a full-screen TUI turns on. Written straight to the terminal
#: on a fatal signal, because a crash skips Textual's own restore and leaves the
#: terminal in raw mouse-reporting mode: every mouse movement then arrives at the
#: shell as text like ``35;72;10M``, and the shell tries to run it as a command.
#:
#: In order: leave the alternate screen, show the cursor, disable bracketed
#: paste, and turn off all four mouse-reporting modes.
_TERMINAL_RESTORE = (
    b"\x1b[?1049l\x1b[?25h\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"
)


def restore_terminal() -> None:
    """Return the terminal to a usable state.

    Deliberately not called from a signal handler. A Python-level handler for
    SIGSEGV cannot work: Python's C handler only sets a flag and returns, the
    faulting instruction is retried, and the process spins on the same fault
    forever — turning a crash into a hang, which is strictly worse. The
    supervisor in ``run_tui`` calls this from the parent process instead, after
    the child has already died.
    """
    for stream in (sys.__stdout__, sys.__stderr__):
        try:
            if stream is not None and stream.isatty():
                os.write(stream.fileno(), _TERMINAL_RESTORE)
        except (OSError, ValueError):
            continue


def install(root: Path) -> Path | None:
    """Send native-crash stacks to ``.vasuki/logs/crash.log``. Returns the path.

    Failing to install is never worth crashing over — a read-only or missing
    log directory just means crashes stay as quiet as they were before.
    """
    global _handle
    if _handle is not None:
        return Path(_handle.name)
    try:
        directory = paths.state_dir(root) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "crash.log"
        handle = path.open("a", encoding="utf-8", buffering=1)
        handle.write(
            f"\n=== daino started {datetime.now(UTC).isoformat()} pid={os.getpid()} ===\n"
        )
        # faulthandler's handler is written in C: it dumps the stack and then
        # re-raises the signal properly. That is the only safe way to react to a
        # SIGSEGV from Python.
        faulthandler.enable(file=handle, all_threads=True)
        _handle = handle
        return path
    except OSError:
        return None


def note() -> str:
    """One line telling the user where a crash stack would have been written."""
    if _handle is None:
        return ""
    return f"A native crash would be recorded in {_handle.name}"


def dump_now(message: str = "") -> None:
    """Write the current stacks on demand, for diagnosing a hang rather than a crash."""
    if _handle is None:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        return
    if message:
        _handle.write(f"--- {message} {datetime.now(UTC).isoformat()} ---\n")
    faulthandler.dump_traceback(file=_handle, all_threads=True)
