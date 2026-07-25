from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vasuki.exceptions import PolicyDenied
from vasuki.runtimes import DockerRuntime, LocalRuntime
from vasuki.schemas import CommandResult
from vasuki.security import PolicyEngine
from vasuki.tools import EditTools, FileTools
from vasuki.verification import RepairLoop, VerificationEngine


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

    monkeypatch.setattr("vasuki.runtimes.docker.shutil.which", lambda name: "/usr/bin/docker")
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
    assert "--cpus 1" in commands[-1]
    assert "--memory 512m" in commands[-1]
    assert "project-tests" in commands[-1]


@pytest.mark.asyncio
async def test_browser_verifier_reports_optional_dependency(tmp_path: Path) -> None:
    from vasuki.verification import BrowserVerifier

    report = await BrowserVerifier().verify("http://example.invalid", artifact_dir=tmp_path)
    assert not report.passed
    assert report.error is not None
