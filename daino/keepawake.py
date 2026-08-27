"""Keep the machine awake while the agent is working.

A mission can run for many minutes with no keyboard or mouse activity, so the
display sleeps and the host may suspend — which pauses the very work the user is
waiting for, drops the model's HTTP connections, and freezes a container mid
command. Both clients therefore hold an OS sleep inhibitor for the duration of a
turn.

Refcounted, because a chat turn, a QA scan, and a preview server can overlap; the
inhibitor is released only when the last of them finishes. Every failure mode is
a no-op: a host without an inhibitor still runs the turn, it just sleeps.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Iterator

from daino import branding

logger = logging.getLogger(__name__)

#: Set to "off"/"0" to never inhibit sleep, whatever the configuration says.
#: Named to avoid the settings model's ``DAINO_``-prefixed field names — the
#: configuration flag itself is overridable as ``DAINO_KEEP_AWAKE``.
ENV_SWITCH = "DAINO_WAKELOCK"

# Windows SetThreadExecutionState flags.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def _enabled_by_environment() -> bool:
    value = os.environ.get(ENV_SWITCH, "").strip().casefold()
    return value not in {"off", "0", "false", "no"}


class KeepAwake:
    """A refcounted, cross-platform "do not sleep while I work" handle."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._holders = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._windows_held = False
        #: Recorded so a caller (and a test) can see what actually happened.
        self.mechanism = ""

    @property
    def active(self) -> bool:
        return self._holders > 0

    def acquire(self, reason: str = "") -> None:
        if not self.enabled or not _enabled_by_environment():
            return
        with self._lock:
            self._holders += 1
            if self._holders > 1:
                return
            self._engage(reason or f"{branding.NAME} is working")

    def release(self) -> None:
        if not self.enabled or not _enabled_by_environment():
            return
        with self._lock:
            if self._holders == 0:
                return
            self._holders -= 1
            if self._holders == 0:
                self._disengage()

    @contextlib.contextmanager
    def active_for(self, reason: str = "") -> Iterator[None]:
        """Hold the inhibitor for the duration of a block."""
        self.acquire(reason)
        try:
            yield
        finally:
            self.release()

    def shutdown(self) -> None:
        """Drop the inhibitor unconditionally, however many holders remain."""
        with self._lock:
            self._holders = 0
            self._disengage()

    # ------------------------------------------------------------- mechanisms

    def _engage(self, reason: str) -> None:
        system = platform.system()
        try:
            if system == "Darwin":
                # -d display, -i idle sleep, -m disk, -s system sleep on AC,
                # -u "user is active", so the screen stays lit rather than only
                # the machine staying powered.
                caffeinate = shutil.which("caffeinate")
                if not caffeinate:
                    return
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv
                    [caffeinate, "-dimsu"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.mechanism = "caffeinate"
            elif system == "Linux":
                inhibit = shutil.which("systemd-inhibit")
                if not inhibit:
                    return
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv
                    [
                        inhibit,
                        "--what=idle:sleep",
                        f"--who={branding.NAME}",
                        f"--why={reason}",
                        "--mode=block",
                        "sleep",
                        "infinity",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.mechanism = "systemd-inhibit"
            elif system == "Windows":
                kernel32 = getattr(ctypes, "windll", None)
                if kernel32 is None:
                    return
                kernel32.kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
                )
                self._windows_held = True
                self.mechanism = "SetThreadExecutionState"
        except (OSError, AttributeError) as exc:
            logger.debug("Could not inhibit sleep: %s", exc)
            self._process = None

    def _disengage(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        if self._windows_held:
            self._windows_held = False
            with contextlib.suppress(OSError, AttributeError):
                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS
                )
        self.mechanism = ""
