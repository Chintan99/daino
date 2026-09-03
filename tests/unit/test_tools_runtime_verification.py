from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from daino.application.context import ProjectContext
from daino.application.verification_service import VerificationApplicationService
from daino.events import EventBus
from daino.events import TestsCompleted as VerificationCompletedEvent
from daino.events import TestsStarted as VerificationStartedEvent
from daino.exceptions import PolicyDenied
from daino.missions import MissionService
from daino.runtimes import DockerRuntime, LocalRuntime
from daino.schemas import CommandResult
from daino.security import PolicyEngine
from daino.tools import EditTools, FileTools
from daino.verification import RepairLoop, VerificationEngine


def test_file_tools_confine_workspace(tmp_path: Path) -> None:
    tools = FileTools(tmp_path)
    assert tools.write_file("safe.txt", "ok", create=True).success
    assert tools.read_file("safe.txt").data["content"] == "ok"
    assert not tools.read_file("../outside").success


def test_patch_application_and_syntax_validation(git_repo: Path) -> None:
    path = git_repo / "module.py"
    path.write_text("def answer():\n    return 1\n", encoding="utf-8")
    from tests.conftest import git

    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "module")
    patch = (
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def answer():\n"
        "-    return 1\n"
        "+    return 42\n"
    )
    result = EditTools(git_repo, ["module.py"]).apply_unified_diff(patch)
    assert result.success
    assert "42" in path.read_text(encoding="utf-8")
    disallowed = patch.replace("module.py", "other.py")
    assert not EditTools(git_repo, ["module.py"]).apply_unified_diff(disallowed).success


@pytest.mark.asyncio
async def test_local_runtime_policy_and_timeout(tmp_path: Path) -> None:
    runtime = LocalRuntime(tmp_path, PolicyEngine(), timeout=1)
    result = await runtime.execute(f"{sys.executable} -c 'print(42)'")
    assert result.succeeded
    assert result.stdout.strip() == "42"
    with pytest.raises(PolicyDenied):
        await runtime.execute("rm -rf anything")
    timed = await runtime.execute(f"{sys.executable} -c 'import time; time.sleep(2)'")
    assert timed.timed_out


@pytest.mark.asyncio
async def test_missing_executable_is_a_structured_verification_failure(tmp_path: Path) -> None:
    runtime = LocalRuntime(tmp_path)

    result = await runtime.execute("daino-command-that-does-not-exist --check")
    report = await VerificationEngine(tmp_path, runtime).run(
        ["daino-command-that-does-not-exist --check"]
    )

    assert result.exit_code == 127
    assert "Executable not found" in result.stderr
    assert not report.passed
    assert report.failures[0].failure_type == "Missing dependency or file"


@pytest.mark.asyncio
async def test_local_runtime_imports_the_mission_src_tree_first(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "mission_module.py").write_text("VALUE = 42\n", encoding="utf-8")

    result = await LocalRuntime(tmp_path).execute(
        f"{sys.executable} -c 'import mission_module; assert mission_module.VALUE == 42'"
    )

    assert result.succeeded, result.stderr


@pytest.mark.asyncio
async def test_verification_and_bounded_repair(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    runtime = LocalRuntime(tmp_path)
    engine = VerificationEngine(tmp_path, runtime)
    repairs = 0

    async def repair(failure: object, attempt: int, escalated: bool) -> bool:
        nonlocal repairs
        repairs += 1
        marker.write_text("ok", encoding="utf-8")
        return True

    command = f"{sys.executable} -c 'from pathlib import Path; assert Path(\"{marker}\").exists()'"
    report, attempts = await RepairLoop(engine, local_attempts=1, total_attempts=2).run(
        [command], repair
    )
    assert report.passed
    assert attempts == repairs == 1


@pytest.mark.asyncio
async def test_docker_runtime_builds_isolated_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[str] = []

    async def fake_local_execute(
        runtime: LocalRuntime,
        command: str,
        *,
        timeout: int | None = None,
        approved: bool = False,
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(
            command=command,
            exit_code=0,
            stdout="27.0",
            stderr="",
            duration_seconds=0,
        )

    monkeypatch.setattr("daino.runtimes.docker.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(LocalRuntime, "execute", fake_local_execute)
    runtime = DockerRuntime(
        tmp_path,
        image="project-tests",
        cpu_limit=1,
        memory_limit="512m",
        network_access=False,
    )
    await runtime.prepare()
    result = await runtime.execute("pytest tests/unit")
    assert result.succeeded
    assert "--network none" in commands[-1]
    assert f"--user {os.getuid()}:{os.getgid()}" in commands[-1]
    assert "-e PYTHONDONTWRITEBYTECODE=1" in commands[-1]
    assert "-e PYTHONPATH=/workspace/src" in commands[-1]
    assert "--cpus 1" in commands[-1]
    assert "--memory 512m" in commands[-1]
    assert "project-tests" in commands[-1]

    compose = await runtime.execute("docker compose config")
    assert compose.succeeded
    assert commands[-1] == "docker compose config"


@pytest.mark.asyncio
async def test_docker_runtime_requires_approval_for_host_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daino.exceptions import PolicyDenied

    commands: list[str] = []

    async def fake_local_execute(
        runtime: LocalRuntime,
        command: str,
        *,
        timeout: int | None = None,
        approved: bool = False,
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(
            command=command, exit_code=0, stdout="ok", stderr="", duration_seconds=0
        )

    monkeypatch.setattr(LocalRuntime, "execute", fake_local_execute)
    runtime = DockerRuntime(tmp_path)

    with pytest.raises(PolicyDenied, match="host Docker mutation"):
        await runtime.execute("docker compose build")
    result = await runtime.execute("docker compose build", approved=True)

    assert result.succeeded
    assert commands == ["docker compose build"]


@pytest.mark.asyncio
async def test_application_verification_uses_the_session_approver(
    project: tuple[Path, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings, database = project
    events = EventBus()
    context = ProjectContext(root, settings, database, events)  # type: ignore[arg-type]
    seen_events: list[object] = []
    events.subscribe(seen_events.append)
    approvals: list[tuple[str, str]] = []

    class Runtime:
        commands: list[tuple[str, bool]] = []

        async def prepare(self) -> None: ...

        async def cleanup(self) -> None: ...

        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            self.commands.append((command, approved))
            return CommandResult(
                command=command,
                exit_code=0,
                stdout="built",
                stderr="",
                duration_seconds=0,
            )

    runtime = Runtime()
    monkeypatch.setattr(MissionService, "_runtime", lambda *args: runtime)

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        approvals.append((command, reason))
        return True, True

    report = await VerificationApplicationService(context).run(
        ["docker compose build"], approve=approve
    )

    assert report.passed
    assert runtime.commands == [("docker compose build", True)]
    assert approvals and approvals[0][0] == "docker compose build"
    assert any(isinstance(event, VerificationStartedEvent) for event in seen_events)
    assert any(
        isinstance(event, VerificationCompletedEvent) and event.passed for event in seen_events
    )


@pytest.mark.asyncio
async def test_application_verification_closes_test_state_when_runtime_start_fails(
    project: tuple[Path, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings, database = project
    events = EventBus()
    context = ProjectContext(root, settings, database, events)  # type: ignore[arg-type]
    seen_events: list[object] = []
    events.subscribe(seen_events.append)

    class Runtime:
        cleaned = False

        async def prepare(self) -> None:
            raise RuntimeError("runtime unavailable")

        async def cleanup(self) -> None:
            self.cleaned = True

        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            raise AssertionError("execute must not be reached")

    runtime = Runtime()
    monkeypatch.setattr(MissionService, "_runtime", lambda *args: runtime)

    with pytest.raises(RuntimeError, match="runtime unavailable"):
        await VerificationApplicationService(context).run(["pytest"])

    assert runtime.cleaned
    assert isinstance(seen_events[-1], VerificationCompletedEvent)
    assert not seen_events[-1].passed


@pytest.mark.asyncio
async def test_application_verification_closes_test_state_when_cleanup_fails(
    project: tuple[Path, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings, database = project
    events = EventBus()
    context = ProjectContext(root, settings, database, events)  # type: ignore[arg-type]
    seen_events: list[object] = []
    events.subscribe(seen_events.append)

    class Runtime:
        async def prepare(self) -> None: ...

        async def cleanup(self) -> None:
            raise RuntimeError("cleanup failed")

        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            return CommandResult(
                command=command,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0,
            )

    monkeypatch.setattr(MissionService, "_runtime", lambda *args: Runtime())

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await VerificationApplicationService(context).run(["pytest"])

    assert isinstance(seen_events[-1], VerificationCompletedEvent)
    assert not seen_events[-1].passed


@pytest.mark.asyncio
async def test_browser_verifier_reports_optional_dependency(tmp_path: Path) -> None:
    from daino.verification import BrowserVerifier

    report = await BrowserVerifier().verify("http://example.invalid", artifact_dir=tmp_path)
    assert not report.passed
    assert report.error is not None


def test_a_check_whose_own_program_is_absent_is_named() -> None:
    """The field case: ``git diff --check`` in an image that ships no Git.

    Reported as a verification failure, it told the user their finished edit was
    broken when in fact the check had never run.
    """
    from daino.verification import missing_executable

    assert missing_executable("git diff --check", "sh: 1: git: not found") == "git"
    assert missing_executable("node --check app.js", "bash: node: command not found") == "node"


def test_a_failure_inside_a_check_is_not_a_missing_program() -> None:
    """Otherwise a genuine failure would be excused as a runtime gap."""
    from daino.verification import missing_executable

    assert missing_executable("pytest -q", "ModuleNotFoundError: No module named 'app'") == ""
    assert missing_executable("pytest -q", "FileNotFoundError: config.json: not found") == ""
    assert missing_executable("pytest -q", "sh: 1: git: not found") == ""


# ------------------------- a verification command a model wrote, on a real host


def test_a_model_written_python_command_runs_on_a_host_without_python(
    tmp_path: Path,
) -> None:
    """The last place a bare `python` could still reach the runtime.

    `discover_commands` has never used the bare name, but the planner writes
    verification commands too, and it writes what it has read a thousand times:
    `python test_app.py`. That program does not exist on a modern macOS or most
    Linux distributions. The mission then dies on "Executable not found:
    python" — reported as a *verification failure*, so the user is told their
    finished, correct change did not pass its tests.

    Observed in the field: a two-task plan where the code was written correctly
    and the run failed anyway on `python test_app.py`.
    """
    from daino.runtimes import LocalRuntime
    from daino.verification.engine import VerificationEngine

    engine = VerificationEngine(tmp_path, LocalRuntime(tmp_path))

    resolved = engine.resolve_interpreter("python test_app.py")

    assert not resolved.startswith("python ")
    assert resolved.endswith(" test_app.py")
    assert Path(shlex.split(resolved)[0]).name.startswith("python")


def test_a_project_interpreter_is_preferred_over_daino_s_own(tmp_path: Path) -> None:
    """A project's venv has the dependencies its tests import; Daino's does not."""
    from daino.runtimes import LocalRuntime
    from daino.verification.engine import VerificationEngine

    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    engine = VerificationEngine(tmp_path, LocalRuntime(tmp_path))

    assert engine.resolve_interpreter("python -m pytest").startswith(str(venv / "python"))


def test_every_other_command_is_left_exactly_alone(tmp_path: Path) -> None:
    """Rewriting more than the one broken case would be its own bug."""
    from daino.runtimes import LocalRuntime
    from daino.verification.engine import VerificationEngine

    engine = VerificationEngine(tmp_path, LocalRuntime(tmp_path))

    for command in ("pytest -q", "npm test", "python3 -m pytest", "go test ./...", ""):
        assert engine.resolve_interpreter(command) == command
    # An unparseable command is passed through for `runnable` to reject.
    assert engine.resolve_interpreter("python 'unclosed") == "python 'unclosed"


@pytest.mark.asyncio
async def test_the_normalisation_happens_before_the_command_runs(tmp_path: Path) -> None:
    """It has to cover every caller, so it lives in `run` rather than at one site."""
    from daino.verification.engine import VerificationEngine

    ran: list[str] = []

    async def record(command: str) -> CommandResult:
        ran.append(command)
        return CommandResult(
            command=command, exit_code=0, stdout="", stderr="", duration_seconds=0.0
        )

    engine = VerificationEngine(tmp_path, LocalRuntime(tmp_path), execute=record)

    report = await engine.run(["python test_app.py"])

    assert report.passed
    assert ran and not ran[0].startswith("python ")
