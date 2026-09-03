"""Desktop notifications for work that finished, failed, or needs approval.

An agent turn can run for minutes. The user is expected to go and do something
else, which means the interesting moments — it finished, it broke, it is waiting
for a command approval — arrive when nobody is looking at the window. Both
clients therefore raise a real OS notification rather than only drawing
something in their own UI.

This lives outside the TUI and the GUI on purpose: it is called from the
application service both of them drive, so the terminal client and the browser
notify identically and neither can drift.

Nothing here ever raises. A missing notifier is a missing convenience, not a
failed turn.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum

from daino import branding
from daino.config.models import NotificationsConfig

logger = logging.getLogger(__name__)

#: Set to "off"/"0" to silence every notifier — used by the test suite, and by
#: anyone running Daino somewhere a popup would be unwelcome. Deliberately not
#: ``DAINO_NOTIFICATIONS``: the settings model reads ``DAINO_``-prefixed names as
#: configuration fields, and that one is the notifications *section*.
ENV_SWITCH = "DAINO_NOTIFY"

#: Notifications are a courtesy; a notifier that hangs must not hold up a turn.
_TIMEOUT_SECONDS = 5


class NotificationKind(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL = "approval"


@dataclass(frozen=True, slots=True)
class Notification:
    kind: NotificationKind
    title: str
    body: str


def _enabled_by_environment() -> bool:
    value = os.environ.get(ENV_SWITCH, "").strip().casefold()
    return value not in {"off", "0", "false", "no"}


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class NotificationService:
    """Raise OS notifications for the three moments worth interrupting for."""

    def __init__(
        self,
        config: NotificationsConfig | None = None,
        *,
        stream: object | None = None,
    ) -> None:
        self.config = config or NotificationsConfig()
        #: Where the terminal bell is written. The TUI owns the screen, so this
        #: is deliberately just the bell character and never cursor movement.
        self._stream = stream if stream is not None else sys.stdout

    # ------------------------------------------------------------------ policy

    def wants(self, kind: NotificationKind) -> bool:
        if not self.config.enabled or not _enabled_by_environment():
            return False
        return {
            NotificationKind.COMPLETED: self.config.on_completed,
            NotificationKind.FAILED: self.config.on_failed,
            NotificationKind.APPROVAL: self.config.on_approval,
        }[kind]

    # ------------------------------------------------------------- entrypoints

    def completed(self, body: str, *, title: str = "") -> Notification | None:
        return self.send(NotificationKind.COMPLETED, title or f"{branding.NAME} finished", body)

    def failed(self, body: str, *, title: str = "") -> Notification | None:
        return self.send(NotificationKind.FAILED, title or f"{branding.NAME} needs you", body)

    def approval(self, body: str, *, title: str = "") -> Notification | None:
        return self.send(NotificationKind.APPROVAL, title or f"{branding.NAME} is waiting", body)

    def send(self, kind: NotificationKind, title: str, body: str) -> Notification | None:
        """Deliver one notification, returning what was sent (or ``None``)."""
        if not self.wants(kind):
            return None
        notification = Notification(kind=kind, title=title, body=_one_line(body))
        if self.config.desktop:
            self._desktop(notification)
        if self.config.terminal_bell:
            self._bell()
        return notification

    # -------------------------------------------------------------- delivery

    def _desktop(self, notification: Notification) -> None:
        """Dispatch the platform notifier without waiting for it.

        Called from a TUI event handler and from an async turn, so it must not
        block: a notifier that takes a second to appear would stutter the
        interface it is reporting about. The work runs on a daemon thread, which
        also keeps the child process reaped.
        """
        command = self._desktop_command(notification)
        if not command:
            return

        def deliver() -> None:
            try:
                subprocess.run(  # noqa: S603 - fixed argv, no shell
                    command,
                    check=False,
                    capture_output=True,
                    timeout=_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("Desktop notification failed: %s", exc)

        threading.Thread(target=deliver, name="daino-notify", daemon=True).start()

    @staticmethod
    def _desktop_command(notification: Notification) -> list[str] | None:
        """The platform's notifier, or ``None`` when the host has none."""
        system = platform.system()
        if system == "Darwin":
            # terminal-notifier, when installed, gives the notification an app
            # identity and makes it clickable; osascript is always present.
            notifier = shutil.which("terminal-notifier")
            if notifier:
                return [
                    notifier,
                    "-title",
                    notification.title,
                    "-message",
                    notification.body,
                    "-group",
                    "daino",
                ]
            script = (
                f'display notification "{_escape_applescript(notification.body)}"'
                f' with title "{_escape_applescript(notification.title)}"'
            )
            return ["osascript", "-e", script]
        if system == "Linux":
            notifier = shutil.which("notify-send")
            if not notifier:
                return None
            urgency = "critical" if notification.kind != NotificationKind.COMPLETED else "normal"
            return [
                notifier,
                "--app-name",
                branding.NAME,
                "--urgency",
                urgency,
                notification.title,
                notification.body,
            ]
        if system == "Windows":
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            if not powershell:
                return None
            # BurntToast is not assumed; the balloon API is always available.
            script = (
                "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')"
                ">$null; $n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip(5000,'{notification.title}','{notification.body}',"
                "[System.Windows.Forms.ToolTipIcon]::Info)"
            )
            return [powershell, "-NoProfile", "-Command", script]
        return None

    def _bell(self) -> None:
        """Ring the terminal bell, which most terminals turn into a badge."""
        stream = self._stream
        try:
            if not hasattr(stream, "write"):
                return
            # Only a real terminal; writing control characters into a pipe or a
            # captured log is noise, not a notification.
            if hasattr(stream, "isatty") and not stream.isatty():
                return
            stream.write("\a")
            if hasattr(stream, "flush"):
                stream.flush()
        except (OSError, ValueError) as exc:
            logger.debug("Terminal bell failed: %s", exc)


def _one_line(value: str, limit: int = 180) -> str:
    """Notifications are one line; a stack trace in a toast helps nobody."""
    collapsed = " ".join(str(value).split())
    return collapsed[: limit - 1] + "…" if len(collapsed) > limit else collapsed
