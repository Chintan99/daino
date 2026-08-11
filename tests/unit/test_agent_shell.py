"""The agent's shell: what it may run unattended, what it must ask about, what is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from vasuki.agents.tool_schemas import CHAT_TOOL_SPECS
from vasuki.config.models import SecurityConfig
from vasuki.schemas import AgentAction, CommandResult, EditSpec, TodoItem
from vasuki.security.commands import CommandGate, Verdict
from vasuki.tools import ActionExecutor, EditTools
from vasuki.tools.commands import CommandRunner


class FakeRuntime:
    """Records what it was asked to run instead of running it."""

    def __init__(self, *, stdout: str = "ok", exit_code: int = 0) -> None:
        self.calls: list[str] = []
        self.stdout = stdout
        self.exit_code = exit_code

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        self.calls.append(command)
        return CommandResult(
            command=command,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr="" if self.exit_code == 0 else "boom",
            duration_seconds=0.01,
        )


def runner(
    runtime: FakeRuntime,
    *,
    approve: object = None,
    config: SecurityConfig | None = None,
) -> CommandRunner:
    return CommandRunner(
        runtime,  # type: ignore[arg-type]
        CommandGate(config or SecurityConfig()),
        approve=approve,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", Verdict.ALLOW),
        ("ruff check .", Verdict.ALLOW),
        ("python -m pytest", Verdict.ALLOW),
        ("git status", Verdict.ALLOW),
        # Reading git is routine; publishing or discarding work is not.
        ("git push origin main", Verdict.ASK),
        ("git reset --hard", Verdict.ASK),
        ("docker compose config", Verdict.ALLOW),
        ("docker info", Verdict.ALLOW),
        ("docker compose build", Verdict.ASK),
        ("docker compose up -d", Verdict.ASK),
        ("docker system prune", Verdict.DENY),
        ("pip install httpx", Verdict.ASK),
        ("npm install", Verdict.ASK),
        ("curl https://example.invalid", Verdict.ASK),
        ("rm -rf build", Verdict.DENY),
        ("mkfs /dev/sda", Verdict.DENY),
    ],
)
def test_gate_classifies_commands(command: str, expected: Verdict) -> None:
    assert CommandGate().decide(command).verdict is expected


def test_a_destructive_command_can_never_be_approved() -> None:
    """DENY is not a prompt. There is no answer that runs a recursive delete."""
    gate = CommandGate()
    decision = gate.decide("rm -rf /")
    gate.remember(decision.signature)

    assert gate.decide("rm -rf /").verdict is Verdict.DENY


def test_remembering_an_approval_covers_the_same_command_not_its_siblings() -> None:
    gate = CommandGate()
    gate.remember(gate.decide("pip install httpx").signature)

    assert gate.decide("pip install requests").verdict is Verdict.ALLOW
    # Approving installs must not quietly approve removals.
    assert gate.decide("pip uninstall requests").verdict is Verdict.ASK


def test_project_config_can_widen_and_narrow_the_safe_set() -> None:
    widened = CommandGate(SecurityConfig(allowed_commands=["npm"]))
    narrowed = CommandGate(SecurityConfig(denied_commands=["pytest"]))

    assert widened.decide("npm test").verdict is Verdict.ALLOW
    assert narrowed.decide("pytest -q").verdict is not Verdict.ALLOW


def test_shell_syntax_is_refused_with_an_explanation() -> None:
    decision = CommandGate().decide("pytest -q | head -5")
    assert decision.verdict is Verdict.DENY
    assert "shell syntax is not available" in decision.reason


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_safe_command_runs_without_asking() -> None:
    runtime = FakeRuntime(stdout="3 passed")
    asked: list[str] = []

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        asked.append(command)
        return True, False

    result = await runner(runtime, approve=approve).run("pytest -q")

    assert result.success
    assert result.data["stdout"] == "3 passed"
    assert runtime.calls == ["pytest -q"]
    assert asked == []


@pytest.mark.asyncio
async def test_an_install_asks_first_and_runs_when_allowed() -> None:
    runtime = FakeRuntime()
    asked: list[tuple[str, str]] = []

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        asked.append((command, reason))
        return True, False

    result = await runner(runtime, approve=approve).run("pip install httpx")

    assert result.success
    assert runtime.calls == ["pip install httpx"]
    assert asked[0][0] == "pip install httpx"
    assert "approval" in asked[0][1]


@pytest.mark.asyncio
async def test_declining_an_install_does_not_run_it() -> None:
    runtime = FakeRuntime()

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        return False, False

    result = await runner(runtime, approve=approve).run("pip install httpx")

    assert not result.success
    assert "declined" in (result.error or "")
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_answering_always_stops_the_next_one_asking() -> None:
    runtime = FakeRuntime()
    asked: list[str] = []

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        asked.append(command)
        return True, True

    shell = runner(runtime, approve=approve)
    await shell.run("pip install httpx")
    await shell.run("pip install requests")

    assert asked == ["pip install httpx"]
    assert runtime.calls == ["pip install httpx", "pip install requests"]


@pytest.mark.asyncio
async def test_a_destructive_command_is_refused_without_prompting() -> None:
    runtime = FakeRuntime()
    asked: list[str] = []

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        asked.append(command)
        return True, True

    result = await runner(runtime, approve=approve).run("rm -rf /")

    assert not result.success
    assert "cannot be approved" in (result.error or "")
    assert asked == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_without_an_approver_a_gated_command_is_not_run() -> None:
    """Headless callers must not silently gain permissions the TUI would ask for."""
    runtime = FakeRuntime()

    result = await runner(runtime).run("pip install httpx")

    assert not result.success
    assert "no approver" in (result.error or "")
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_failing_command_is_an_observation_not_an_exception() -> None:
    result = await runner(FakeRuntime(exit_code=1, stdout="F")).run("pytest -q")

    assert not result.success
    assert result.data["exit_code"] == 1
    assert result.error


@pytest.mark.asyncio
async def test_a_missing_tool_in_docker_names_the_sandbox_and_compose_alternative() -> None:
    class Missing(FakeRuntime):
        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            return CommandResult(
                command=command,
                exit_code=127,
                stdout="",
                stderr="sh: 1: npm: not found",
                duration_seconds=0.01,
            )

    async def approve(command: str, reason: str) -> tuple[bool, bool]:
        return True, False

    result = await CommandRunner(
        Missing(),  # type: ignore[arg-type]
        CommandGate(),
        runtime_name="docker",
        approve=approve,
    ).run("npm --version")

    assert not result.success
    assert "configured Docker sandbox image" in (result.error or "")
    assert "docker compose" in (result.error or "")
    assert result.data["runtime"] == "docker"


@pytest.mark.asyncio
async def test_long_output_keeps_the_head_and_the_tail() -> None:
    """A failure is usually at one end; the middle of a build log rarely matters."""
    runtime = FakeRuntime(stdout="A" * 5_000 + "MIDDLE" + "Z" * 5_000)

    result = await runner(runtime).run("pytest -q")

    stdout = result.data["stdout"]
    assert len(stdout) < 11_006
    assert stdout.startswith("A")
    assert stdout.endswith("Z")
    assert "characters trimmed" in stdout
    assert "MIDDLE" not in stdout


@pytest.mark.asyncio
async def test_an_unavailable_runtime_explains_itself_once() -> None:
    shell = CommandRunner(
        FakeRuntime(),  # type: ignore[arg-type]
        CommandGate(),
        unavailable="Commands cannot run: the docker runtime failed to start.",
    )

    result = await shell.run("pytest -q")

    assert not result.success
    assert "docker runtime failed to start" in (result.error or "")


# --------------------------------------------------------------------------
# Reaching it through the agent's action space
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_executor_runs_commands_when_a_runner_is_attached(tmp_path: Path) -> None:
    runtime = FakeRuntime(stdout="2 passed")
    executor = ActionExecutor(EditTools(tmp_path), runner(runtime))

    result, paths = await executor.execute(
        AgentAction(thought="verify", action="run_command", command="pytest -q")
    )

    assert result.success
    assert paths == []
    assert runtime.calls == ["pytest -q"]


@pytest.mark.asyncio
async def test_without_a_runner_the_agent_is_told_plainly(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))

    result, _ = await executor.execute(
        AgentAction(thought="verify", action="run_command", command="pytest -q")
    )

    assert not result.success
    assert "not available" in (result.error or "")


def test_run_command_is_in_the_chat_action_space() -> None:
    names = {spec["function"]["name"] for spec in CHAT_TOOL_SPECS}
    assert {
        "run_command",
        "resolve_command_failure",
        "glob",
        "grep",
        "multi_edit",
        "todo",
    } <= names


# --------------------------------------------------------------------------
# The other new tools
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_edit_applies_every_span_to_one_file(tmp_path: Path) -> None:
    target = tmp_path / "page.html"
    target.write_text("<h1>Old</h1>\n<p>Body</p>\n<footer>Old</footer>\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path))

    result, paths = await executor.execute(
        AgentAction(
            thought="restyle",
            action="multi_edit",
            path="page.html",
            edits=[
                EditSpec(old_string="<h1>Old</h1>", new_string='<h1 class="g">New</h1>'),
                EditSpec(old_string="<p>Body</p>", new_string='<p class="g">Body</p>'),
            ],
        )
    )

    assert result.success
    assert result.data["edits"] == 2
    assert paths == ["page.html"]
    text = target.read_text(encoding="utf-8")
    assert 'class="g">New' in text and 'class="g">Body' in text
    # The untouched span is left alone.
    assert "<footer>Old</footer>" in text


@pytest.mark.asyncio
async def test_multi_edit_failure_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("keep\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path))

    result, paths = await executor.execute(
        AgentAction(
            thought="t",
            action="multi_edit",
            path="a.txt",
            edits=[
                EditSpec(old_string="keep", new_string="kept"),
                EditSpec(old_string="missing anchor", new_string="x"),
            ],
        )
    )

    assert not result.success
    assert "Edit 2 of 2 failed" in (result.error or "")
    assert "No edits were applied" in (result.error or "")
    assert paths == []
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_read_file_can_page_through_a_large_file(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text(
        "\n".join(f"line{index}" for index in range(1, 101)) + "\n", encoding="utf-8"
    )
    executor = ActionExecutor(EditTools(tmp_path))

    result, _ = await executor.execute(
        AgentAction(thought="t", action="read_file", path="big.py", offset=50, limit=3)
    )

    assert result.success
    assert result.data["content"].splitlines() == ["line50", "line51", "line52"]


@pytest.mark.asyncio
async def test_a_range_covering_the_whole_file_satisfies_read_before_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / "small.py"
    target.write_text("value = 1\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=True))

    await executor.execute(
        AgentAction(thought="read", action="read_file", path="small.py", offset=1, limit=200)
    )
    result, _ = await executor.execute(
        AgentAction(
            thought="edit",
            action="replace",
            path="small.py",
            old_string="value = 1",
            new_string="value = 2",
        )
    )

    assert result.success
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_paging_through_a_file_satisfies_read_before_write(tmp_path: Path) -> None:
    target = tmp_path / "paged.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=True))

    await executor.execute(
        AgentAction(thought="read", action="read_file", path="paged.txt", offset=1, limit=2)
    )
    blocked, _ = await executor.execute(
        AgentAction(
            thought="edit",
            action="replace",
            path="paged.txt",
            old_string="four",
            new_string="FOUR",
        )
    )
    await executor.execute(
        AgentAction(thought="read", action="read_file", path="paged.txt", offset=3, limit=2)
    )
    applied, _ = await executor.execute(
        AgentAction(
            thought="edit",
            action="replace",
            path="paged.txt",
            old_string="four",
            new_string="FOUR",
        )
    )

    assert not blocked.success
    assert applied.success
    assert target.read_text(encoding="utf-8").endswith("FOUR\n")


@pytest.mark.asyncio
async def test_agent_can_refine_a_file_it_just_created_without_rereading(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=True))

    created, _ = await executor.execute(
        AgentAction(thought="create", action="write", path="new.py", content="value = 1\n")
    )
    refined, _ = await executor.execute(
        AgentAction(
            thought="refine",
            action="replace",
            path="new.py",
            old_string="value = 1",
            new_string="value = 2",
        )
    )

    assert created.success and refined.success
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_glob_and_grep_find_files_and_patterns(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("const beta = 1\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path))

    found, _ = await executor.execute(
        AgentAction(thought="t", action="glob", pattern="src/**/*.py")
    )
    matched, _ = await executor.execute(
        AgentAction(thought="t", action="grep", query=r"^def \w+", pattern="src/**/*.py")
    )

    assert found.data["matches"] == ["src/a.py"]
    assert [item["path"] for item in matched.data["matches"]] == ["src/a.py"]


@pytest.mark.asyncio
async def test_todo_records_the_plan(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))

    result, _ = await executor.execute(
        AgentAction(
            thought="plan",
            action="todo",
            todos=[
                TodoItem(content="read the page", status="completed"),
                TodoItem(content="restyle it", status="in_progress"),
                TodoItem(content="run the tests", status="pending"),
            ],
        )
    )

    assert result.success
    assert [item.status for item in executor.todos] == ["completed", "in_progress", "pending"]
    assert result.data["todos"][1]["content"] == "restyle it"


# --------------------------------------------------------------------------
# Runtime selection and unusable runtimes
# --------------------------------------------------------------------------


def test_a_new_project_gets_a_runtime_the_machine_can_use(tmp_path: Path) -> None:
    """Defaulting to docker made every command fail wherever docker is unreachable."""
    from vasuki.config.models import RuntimeConfig
    from vasuki.runtimes.detect import preferred_runtime

    assert RuntimeConfig().default == "local"
    assert preferred_runtime() == "local"


def test_docker_permission_trouble_names_the_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """'permission denied' alone leaves the user with nowhere to go."""
    import subprocess

    from vasuki.runtimes import detect

    monkeypatch.setattr(detect.shutil, "which", lambda name: "/usr/bin/docker")

    def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied ... docker.sock\n"
        )

    monkeypatch.setattr(detect.subprocess, "run", denied)

    usable, reason = detect.docker_status()

    assert not usable
    assert "usermod -aG docker" in reason
    assert detect.preferred_runtime() == "local"


def test_a_missing_docker_binary_is_reported_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    from vasuki.runtimes import detect

    monkeypatch.setattr(detect.shutil, "which", lambda name: None)
    usable, reason = detect.docker_status()

    assert not usable
    assert reason == "Docker is not installed."


@pytest.mark.asyncio
async def test_a_silent_failure_reports_status_and_runtime_not_command_failed() -> None:
    """'command failed' names neither the cause nor anything to do about it."""

    class Silent(FakeRuntime):
        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            return CommandResult(
                command=command, exit_code=125, stdout="", stderr="", duration_seconds=0.01
            )

    result = await CommandRunner(
        Silent(),  # type: ignore[arg-type]
        CommandGate(),
        runtime_name="docker",
    ).run("pytest -q")

    assert not result.success
    assert "exited with status 125" in (result.error or "")
    assert "docker" in (result.error or "")
    assert "command failed" not in (result.error or "")


@pytest.mark.asyncio
async def test_a_timeout_says_so() -> None:
    class Slow(FakeRuntime):
        async def execute(
            self, command: str, *, timeout: int | None = None, approved: bool = False
        ) -> CommandResult:
            return CommandResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr="",
                timed_out=True,
                duration_seconds=30.0,
            )

    result = await CommandRunner(Slow(), CommandGate()).run("pytest -q", timeout=30)  # type: ignore[arg-type]

    assert "timed out after 30s" in (result.error or "")
