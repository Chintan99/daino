"""Notifications and sleep inhibition, which both clients inherit.

The autouse guard in conftest switches both off for the rest of the suite; these
tests turn them back on deliberately and stub the OS commands, so the behaviour
is verified without notifying the developer's desktop or holding their machine
awake.
"""

from __future__ import annotations

import io
import platform
import subprocess
from typing import Any

import pytest

from daino.application.attention import TurnAttention
from daino.config.models import NotificationsConfig, Settings
from daino.keepawake import KeepAwake
from daino.notifications import NotificationKind, NotificationService


@pytest.fixture(autouse=True)
def switches_on(monkeypatch: pytest.MonkeyPatch, no_desktop_side_effects: None) -> None:
    """Turn the features back on for this module.

    Depends on the suite-wide guard explicitly: without that ordering, pytest is
    free to apply the two autouse fixtures in either order and "off" could win.
    """
    monkeypatch.setenv("DAINO_NOTIFY", "on")
    monkeypatch.setenv("DAINO_WAKELOCK", "on")


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of every notifier the service would have run."""
    recorded: list[list[str]] = []

    def fake_thread(target: Any = None, **_: Any) -> Any:
        class Immediate:
            def start(self) -> None:
                target()

        return Immediate()

    def fake_run(argv: list[str], **_: Any) -> Any:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("daino.notifications.threading.Thread", fake_thread)
    monkeypatch.setattr("daino.notifications.subprocess.run", fake_run)
    return recorded


# ------------------------------------------------------------- notifications


def test_each_moment_can_be_switched_off_independently(commands: list[list[str]]) -> None:
    service = NotificationService(NotificationsConfig(on_completed=False, terminal_bell=False))
    assert service.send(NotificationKind.COMPLETED, "t", "b") is None
    assert not commands

    assert service.send(NotificationKind.FAILED, "t", "b") is not None
    assert len(commands) == 1
    assert service.send(NotificationKind.APPROVAL, "t", "b") is not None
    assert len(commands) == 2


def test_the_master_switch_and_the_environment_both_silence_it(
    commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        NotificationService(NotificationsConfig(enabled=False)).send(
            NotificationKind.FAILED, "t", "b"
        )
        is None
    )

    monkeypatch.setenv("DAINO_NOTIFY", "off")
    assert (
        NotificationService(NotificationsConfig()).send(NotificationKind.FAILED, "t", "b") is None
    )
    assert not commands


def test_the_body_is_collapsed_to_one_line(commands: list[list[str]]) -> None:
    service = NotificationService(NotificationsConfig(terminal_bell=False))
    sent = service.failed("tests failed\n  File 'x.py', line 3\n    assert False\n" * 12)
    assert sent is not None
    assert "\n" not in sent.body
    assert len(sent.body) <= 180
    assert sent.body.endswith("…")


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS notifier")
def test_the_macos_notifier_is_given_escaped_text(
    commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("daino.notifications.shutil.which", lambda _: None)
    NotificationService(NotificationsConfig(terminal_bell=False)).completed('say "hi"')
    assert commands and commands[0][0] == "osascript"
    script = commands[0][2]
    # An unescaped quote would end the AppleScript string and change the command.
    assert '\\"hi\\"' in script


def test_the_bell_is_only_written_to_a_terminal(commands: list[list[str]]) -> None:
    class FakePipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    class FakeTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    pipe, terminal = FakePipe(), FakeTerminal()
    config = NotificationsConfig(desktop=False)
    NotificationService(config, stream=pipe).completed("done")
    NotificationService(config, stream=terminal).completed("done")
    # Control characters in a captured log are noise, not a notification.
    assert pipe.getvalue() == ""
    assert terminal.getvalue() == "\a"


def test_a_missing_notifier_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daino.notifications.platform.system", lambda: "Haiku")
    assert NotificationService(NotificationsConfig()).failed("still fine") is not None


# ------------------------------------------------------------- sleep inhibitor


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def inhibitor(monkeypatch: pytest.MonkeyPatch) -> tuple[KeepAwake, list[list[str]]]:
    """A KeepAwake whose platform command is recorded rather than run."""
    started: list[list[str]] = []
    processes: list[FakeProcess] = []

    def fake_popen(argv: list[str], **_: Any) -> FakeProcess:
        started.append(argv)
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("daino.keepawake.platform.system", lambda: "Darwin")
    monkeypatch.setattr("daino.keepawake.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("daino.keepawake.subprocess.Popen", fake_popen)
    keep = KeepAwake()
    keep._test_processes = processes  # type: ignore[attr-defined]
    return keep, started


def test_the_inhibitor_is_refcounted(
    inhibitor: tuple[KeepAwake, list[list[str]]],
) -> None:
    """Overlapping work must not release the inhibitor early."""
    keep, started = inhibitor
    keep.acquire("chat")
    keep.acquire("qa")
    assert len(started) == 1  # one caffeinate for both holders
    assert started[0][1] == "-dimsu"  # display, idle, disk, system, user

    keep.release()
    assert keep.active is True  # the QA scan is still running
    process = keep._test_processes[0]  # type: ignore[attr-defined]
    assert process.terminated is False

    keep.release()
    assert keep.active is False
    assert process.terminated is True


def test_shutdown_drops_every_holder(inhibitor: tuple[KeepAwake, list[list[str]]]) -> None:
    keep, _ = inhibitor
    keep.acquire()
    keep.acquire()
    keep.shutdown()
    assert keep.active is False
    assert keep._test_processes[0].terminated is True  # type: ignore[attr-defined]


def test_a_disabled_inhibitor_never_spawns_anything(
    inhibitor: tuple[KeepAwake, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    keep, started = inhibitor
    keep.enabled = False
    keep.acquire()
    assert not started
    assert keep.active is False

    keep.enabled = True
    monkeypatch.setenv("DAINO_WAKELOCK", "off")
    keep.acquire()
    assert not started


# -------------------------------------------------------------------- turns


@pytest.fixture
def no_real_inhibitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sleep inhibition a no-op without touching `platform`.

    Patching `daino.keepawake.platform.system` would mutate the shared `platform`
    module and silently disable the *notifier* as well — which is exactly the
    trap that made these tests report no notifications at all. Hiding the binary
    is local to the inhibitor, and `acquire()` still refcounts.
    """
    monkeypatch.setattr("daino.keepawake.shutil.which", lambda _: None)


async def test_a_turn_holds_the_inhibitor_and_announces_its_outcome(
    commands: list[list[str]], no_real_inhibitor: None
) -> None:
    attention = TurnAttention(Settings())

    async with attention.turn("Browser turn") as report:
        assert attention.keep_awake.active is True
        report.completed("3 files changed")
    assert attention.keep_awake.active is False
    assert len(commands) == 1

    async with attention.turn("Browser turn") as report:
        report.failed("verification failed")
    assert len(commands) == 2


async def test_a_raising_turn_notifies_and_still_releases(
    commands: list[list[str]], no_real_inhibitor: None
) -> None:
    attention = TurnAttention(Settings())

    with pytest.raises(RuntimeError):
        async with attention.turn("QA scan"):
            raise RuntimeError("provider unreachable")
    assert attention.keep_awake.active is False
    assert len(commands) == 1


async def test_cancelling_a_turn_is_not_worth_a_notification(
    commands: list[list[str]], no_real_inhibitor: None
) -> None:
    """The user pressed stop; they are obviously present."""
    import asyncio

    attention = TurnAttention(Settings())

    with pytest.raises(asyncio.CancelledError):
        async with attention.turn("Browser turn"):
            raise asyncio.CancelledError
    assert not commands
    assert attention.keep_awake.active is False


async def test_an_approval_request_is_announced_before_it_blocks(
    commands: list[list[str]], no_real_inhibitor: None
) -> None:
    """The one moment the agent cannot proceed without the user."""
    attention = TurnAttention(Settings())
    seen: list[str] = []

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        # The notification must already have been sent by the time the client is
        # asked, or a user away from the screen never learns to come back.
        assert len(commands) == 1
        seen.append(command)
        return True, False

    wrapped = attention.watching_approvals(approve)
    assert wrapped is not None
    assert await wrapped("rm -rf build/", "writes outside the workspace") == (True, False)
    assert seen == ["rm -rf build/"]

    assert attention.watching_approvals(None) is None


# ------------------------------------------------------- closing changeset


def test_a_turn_records_one_changeset_summary(tmp_path: Any) -> None:
    """Both clients render the same list of edited files from one message."""
    from daino.application import initialize_project, open_project
    from daino.schemas.core import ChatOutcome, FileDiff

    initialize_project(tmp_path)
    context = open_project(tmp_path)
    try:
        from daino.application.mission_service import MissionApplicationService

        service = MissionApplicationService(context)
        session = service.create_session("changeset")
        outcome = ChatOutcome(
            summary="done",
            diffs=[
                FileDiff(path="README.md", change="modified", added=15, removed=15),
                FileDiff(path="docs/a.svg", change="modified", added=2, removed=2),
                # The same file edited twice in one turn is one row, summed.
                FileDiff(path="README.md", change="modified", added=1, removed=0),
                FileDiff(path="new.py", change="created", added=30, removed=0),
            ],
            changed=["README.md", "docs/a.svg", "new.py"],
        )
        service._record_changeset(session, "mission-1", outcome)

        messages = [item for item in service.messages(session) if item.kind == "changeset"]
        assert len(messages) == 1
        metadata = messages[0].metadata
        assert metadata["added"] == 48
        assert metadata["removed"] == 17
        files = {item["path"]: item for item in metadata["files"]}
        assert set(files) == {"README.md", "docs/a.svg", "new.py"}
        assert files["README.md"]["added"] == 16
        assert files["new.py"]["change"] == "created"
        # Biggest change first: the file to look at is the one at the top.
        # README.md churns 16+15=31 lines, new.py 30, docs/a.svg 4.
        churn = [item["added"] + item["removed"] for item in metadata["files"]]
        assert churn == sorted(churn, reverse=True)
        assert [item["path"] for item in metadata["files"]] == [
            "README.md",
            "new.py",
            "docs/a.svg",
        ]
        # The text body stands alone for the terminal client.
        assert messages[0].content.splitlines()[0] == "Edited 3 files  +48 -17"

        # A turn that changed nothing adds no card.
        service._record_changeset(session, "mission-2", ChatOutcome(answer="just asking"))
        assert len([i for i in service.messages(session) if i.kind == "changeset"]) == 1
    finally:
        context.close()
