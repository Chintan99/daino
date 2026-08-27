"""Sleep inhibition and OS notifications around one agent turn.

Both concerns answer the same question — "the user has walked away, now what?" —
so they live behind one object that the TUI, the browser server, and the CLI all
use. A turn holds a sleep inhibitor while it runs and raises a notification when
it ends or when it stops to ask for an approval.

Keeping this in the application layer rather than in either client is what makes
the two behave identically; neither has to remember to do it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from daino.config.models import Settings
from daino.keepawake import KeepAwake
from daino.notifications import NotificationService

#: What the command layer expects: (command, reason) -> (approved, remember).
ApprovalCallback = Callable[[str, str], Awaitable[tuple[bool, bool]]]


@dataclass
class TurnReport:
    """What the turn wants said when it ends.

    A turn that merely returns is not necessarily a success — a chat turn can
    finish with failing verification — so the caller states the outcome instead
    of it being inferred from the absence of an exception.
    """

    label: str
    outcome: str = "completed"
    detail: str = ""
    #: Set to False for work whose ending is not worth interrupting for.
    announce: bool = True

    def completed(self, detail: str = "") -> None:
        self.outcome = "completed"
        self.detail = detail

    def failed(self, detail: str = "") -> None:
        self.outcome = "failed"
        self.detail = detail

    def silent(self) -> None:
        self.announce = False


@dataclass
class TurnAttention:
    """Per-project sleep inhibitor and notifier."""

    settings: Settings
    notifications: NotificationService = field(init=False)
    keep_awake: KeepAwake = field(init=False)

    def __post_init__(self) -> None:
        self.notifications = NotificationService(self.settings.notifications)
        self.keep_awake = KeepAwake(enabled=self.settings.keep_awake)

    @contextlib.asynccontextmanager
    async def turn(self, label: str) -> AsyncIterator[TurnReport]:
        """Hold the machine awake for a turn, then announce how it ended."""
        report = TurnReport(label=label)
        self.keep_awake.acquire(f"{label} running")
        try:
            yield report
        except BaseException as exc:  # noqa: BLE001 - re-raised after announcing
            # Cancellation is the user stopping the turn themselves; they are
            # obviously present, so there is nothing to interrupt them about.
            if not isinstance(exc, (KeyboardInterrupt, GeneratorExit)) and not _cancelled(exc):
                self.notifications.failed(f"{label} failed: {exc}")
            raise
        else:
            if not report.announce:
                return
            body = report.detail or label
            if report.outcome == "failed":
                self.notifications.failed(body)
            else:
                self.notifications.completed(body)
        finally:
            self.keep_awake.release()

    def watching_approvals(self, approve: ApprovalCallback | None) -> ApprovalCallback | None:
        """Wrap an approval callback so the request is also announced.

        An approval prompt is the one moment the agent cannot make progress
        without the user, which makes it the most valuable of the three
        notifications and the easiest to miss.
        """
        if approve is None:
            return None

        async def announcing(command: str, reason: str) -> tuple[bool, bool]:
            self.notifications.approval(f"Approve: {command}" if command else reason)
            return await approve(command, reason)

        return announcing

    def shutdown(self) -> None:
        self.keep_awake.shutdown()


def _cancelled(exc: BaseException) -> bool:
    import asyncio

    return isinstance(exc, asyncio.CancelledError)
