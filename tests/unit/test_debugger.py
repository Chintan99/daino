"""Debugging over DAP, driven against a real debugpy session.

Not mocked. The things that break in a debugger client are the protocol details
— the configuration handshake, breakpoint verification, frame ids that are only
valid while stopped — and a fake adapter would agree with whatever the client
did. So these launch an actual Python program under debugpy, stop it at a
breakpoint, and read its stack.

Skipped when debugpy is not installed, because the point of the adapter design
is that a project without one still works; the tests that assert *that* run
everywhere.
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.debugger import DebugError, DebugManager, adapters, available, language_of

PROGRAM = textwrap.dedent(
    '''
    def add(left, right):
        total = left + right
        return total


    def main():
        first = add(2, 3)
        second = add(first, 10)
        print(f"result={second}")
        return second


    main()
    '''
).strip()

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _has_debugpy() -> bool:
    try:
        import debugpy  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


needs_debugpy = pytest.mark.skipif(
    not _has_debugpy(), reason="debugpy is not installed in this environment"
)


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "program.py").write_text(PROGRAM + "\n", encoding="utf-8")
    yield tmp_path


async def _stopped(manager: DebugManager, timeout: float = 25.0) -> None:
    """Wait until the debuggee is stopped, or say what it did instead."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        session = manager.session
        if session is not None and session.state == "stopped":
            return
        if session is not None and session.state in {"terminated", "failed"}:
            raise AssertionError(
                f"the debuggee {session.state} without stopping: "
                f"{session.error or ''.join(session.output)[-400:]}"
            )
        await asyncio.sleep(0.05)
    raise AssertionError("the debuggee never stopped at a breakpoint")


# ---------------------------------------------------------------- detection


def test_languages_map_to_the_adapters_that_debug_them() -> None:
    assert language_of("app.py") == "python"
    assert language_of("app.ts") == "typescript"
    # A file no adapter covers gets no language, which is what makes the route
    # answer "unsupported" rather than starting something that cannot work.
    assert language_of("notes.md") == ""


def test_a_missing_adapter_is_reported_with_how_to_install_it(tmp_path: Path) -> None:
    """"No debugger" must never look the same as "the debugger found nothing"."""
    rows = {row["id"]: row for row in available(tmp_path)}

    assert "debugpy" in rows
    assert "pip install debugpy" in str(rows["debugpy"]["install"])
    assert "python" in rows["debugpy"]["languages"]


def test_the_project_interpreter_is_preferred_and_never_bare_python(
    tmp_path: Path,
) -> None:
    """Debugging with a different interpreter than the project runs under
    produces import errors that look like bugs in the code."""
    binary = tmp_path / ".venv" / "bin"
    binary.mkdir(parents=True)
    (binary / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    resolved = adapters.project_python(tmp_path)

    assert resolved == str(binary / "python")
    assert adapters.project_python(Path("/nowhere")) != "python"


def test_free_ports_are_not_handed_out_twice() -> None:
    assert adapters.free_port() != adapters.free_port()


# -------------------------------------------------------------- breakpoints


def test_breakpoints_are_kept_per_file_and_toggle(project: Path) -> None:
    manager = DebugManager(project)

    manager.toggle_breakpoint("program.py", 8)
    manager.toggle_breakpoint("program.py", 3)
    assert [item.line for item in manager.breakpoints["program.py"]] == [3, 8]

    # Toggling the same line again removes it.
    manager.toggle_breakpoint("program.py", 3)
    assert [item.line for item in manager.breakpoints["program.py"]] == [8]

    # A file with no breakpoints left drops out entirely, so `sync` does not
    # keep sending empty sets forever.
    manager.toggle_breakpoint("program.py", 8)
    assert "program.py" not in manager.breakpoints


def test_breakpoints_survive_the_session_that_used_them(project: Path) -> None:
    """They are the user's, not the run's."""
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)

    asyncio.run(manager.stop())

    assert [item.line for item in manager.breakpoints["program.py"]] == [3]
    # But their verification does not: nothing has confirmed them since.
    assert manager.breakpoints["program.py"][0].verified is False


def test_a_condition_is_recorded_against_its_breakpoint(project: Path) -> None:
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)

    manager.set_condition("program.py", 3, "left > 5")

    assert manager.breakpoints["program.py"][0].condition == "left > 5"


def test_launching_with_nothing_named_is_refused(project: Path) -> None:
    manager = DebugManager(project)

    with pytest.raises(DebugError, match="Nothing to debug"):
        asyncio.run(manager.launch())


def test_a_file_no_adapter_covers_is_refused(project: Path) -> None:
    (project / "notes.md").write_text("# hello\n", encoding="utf-8")
    manager = DebugManager(project)

    with pytest.raises(DebugError, match="No debug adapter"):
        asyncio.run(manager.launch(program="notes.md"))


# --------------------------------------------------------------- end to end


@needs_debugpy
async def test_a_breakpoint_stops_the_program_where_it_was_set(project: Path) -> None:
    changes: list[str] = []
    manager = DebugManager(project, on_change=lambda: changes.append("x"))
    manager.toggle_breakpoint("program.py", 3)  # `total = left + right`
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)

        session = manager.session
        assert session is not None
        assert session.stop_reason == "breakpoint"
        # The adapter confirmed the breakpoint rather than the client assuming.
        marker = manager.breakpoints["program.py"][0]
        assert marker.verified is True
        assert marker.actual_line == 3

        frames = await manager.stack()
        assert frames
        assert frames[0].name == "add"
        assert frames[0].path == "program.py"
        assert frames[0].line == 3
        # The caller is below it, which is what makes a call stack useful.
        assert any(frame.name == "main" for frame in frames)
        assert changes  # the panel was told something happened
    finally:
        await manager.stop()


@needs_debugpy
async def test_variables_are_readable_in_the_stopped_frame(project: Path) -> None:
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)
        frames = await manager.stack()

        scopes = await manager.scopes(frames[0].id)
        assert scopes
        locals_scope = next(
            (item for item in scopes if "local" in item.name.casefold()), scopes[0]
        )
        variables = await manager.variables(locals_scope.variables_reference)

        by_name = {item.name: item.value for item in variables}
        assert by_name.get("left") == "2"
        assert by_name.get("right") == "3"
    finally:
        await manager.stop()


@needs_debugpy
async def test_an_expression_is_evaluated_in_the_frame_it_was_asked_about(
    project: Path,
) -> None:
    """"What is `left` here" is the question people ask at a breakpoint."""
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)
        frames = await manager.stack()

        answer = await manager.evaluate("left * 10", frames[0].id)

        assert answer["result"] == "20"
    finally:
        await manager.stop()


@needs_debugpy
async def test_stepping_moves_to_the_next_line(project: Path) -> None:
    manager = DebugManager(project)
    # Line 2 is `total = left + right`; line 3 is `return total`. Stepping from
    # line 3 would correctly leave the function, which is a different assertion.
    manager.toggle_breakpoint("program.py", 2)
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)
        assert (await manager.stack())[0].line == 2

        await manager.step_over()
        await _stopped(manager)

        session = manager.session
        assert session is not None
        assert session.stop_reason == "step"
        assert (await manager.stack())[0].line == 3
    finally:
        await manager.stop()


@needs_debugpy
async def test_stepping_over_a_return_lands_back_in_the_caller(
    project: Path,
) -> None:
    """Stepping out of a frame is not the same as stepping to the next line."""
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)  # `return total`
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)

        await manager.step_over()
        await _stopped(manager)

        frames = await manager.stack()
        assert frames[0].name == "main"
    finally:
        await manager.stop()


@needs_debugpy
async def test_continuing_reaches_the_breakpoint_again(project: Path) -> None:
    """`add` is called twice, so a breakpoint inside it is hit twice."""
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)
        first = await manager.stack()
        assert first[0].line == 3

        await manager.resume()
        await _stopped(manager)

        frames = await manager.stack()
        assert frames[0].line == 3
        # Second call: `left` is now the result of the first.
        answer = await manager.evaluate("left", frames[0].id)
        assert answer["result"] == "5"
    finally:
        await manager.stop()


@needs_debugpy
async def test_a_program_with_no_breakpoints_runs_to_completion(project: Path) -> None:
    manager = DebugManager(project)
    try:
        await manager.launch(program="program.py")
        deadline = asyncio.get_running_loop().time() + 25.0
        while asyncio.get_running_loop().time() < deadline:
            if manager.session and manager.session.state == "terminated":
                break
            await asyncio.sleep(0.05)

        session = manager.session
        assert session is not None
        assert session.state == "terminated"
        # Its output reached the console.
        assert "result=15" in "".join(session.output)
    finally:
        await manager.stop()


@needs_debugpy
async def test_two_sessions_at_once_are_refused(project: Path) -> None:
    """One working tree, one call stack, one panel."""
    manager = DebugManager(project)
    manager.toggle_breakpoint("program.py", 3)
    try:
        await manager.launch(program="program.py")
        await _stopped(manager)

        with pytest.raises(DebugError, match="already running"):
            await manager.launch(program="program.py")
    finally:
        await manager.stop()


@needs_debugpy
async def test_a_breakpoint_on_a_blank_line_is_reported_as_moved_or_refused(
    project: Path,
) -> None:
    """The adapter decides where execution can stop, not the click.

    Drawing the marker where the user clicked while the debugger stops
    elsewhere is the small lie that makes people distrust the whole tool.
    """
    manager = DebugManager(project)
    # Line 5 is blank — between `return total` and the next def.
    manager.toggle_breakpoint("program.py", 5)
    try:
        await manager.launch(program="program.py")
        # Give the adapter a moment to answer setBreakpoints.
        await asyncio.sleep(1.0)

        marker = manager.breakpoints["program.py"][0]
        # Either it was moved to real code, or it was refused — never silently
        # accepted at a line where nothing runs.
        assert marker.moved or not marker.verified
    finally:
        await manager.stop()
