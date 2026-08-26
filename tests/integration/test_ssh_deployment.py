from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daino.config.models import DeploymentTargetConfig
from daino.deployment import DeploymentManager
from daino.exceptions import DeploymentError
from daino.runtimes import RemoteSSHRuntime, Runtime
from daino.schemas import CommandResult


class SSHResult:
    def __init__(self, command: str) -> None:
        self.exit_status = 0
        self.stdout = f"result:{command}"
        self.stderr = ""


class SSHConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, check: bool = False) -> SSHResult:
        self.commands.append(command)
        return SSHResult(command)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ssh_inspection_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = SSHConnection()

    async def connect(**kwargs: Any) -> SSHConnection:
        return connection

    monkeypatch.setattr("asyncssh.connect", connect)
    target = DeploymentTargetConfig(
        type="ssh",
        host="example.invalid",
        username="deployer",
        deployment_path="/opt/apps/test",
    )
    runtime = RemoteSSHRuntime(target)
    await runtime.prepare()
    report = await runtime.inspect()
    await runtime.cleanup()
    assert report["os"]["success"]
    assert connection.commands
    forbidden = ("mkdir", "rm ", "docker compose up", "ln -s", "systemctl start")
    assert not any(token in command for command in connection.commands for token in forbidden)


class FakeDeploymentRuntime(Runtime):
    def __init__(self, *, current: str = "") -> None:
        self.current = current
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []

    async def prepare(self) -> None:
        return None

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        self.commands.append(command)
        stdout = self.current if command.startswith("readlink") else '{"State":"running"}'
        return CommandResult(
            command=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0,
        )

    async def read_file(self, path: str) -> bytes:
        return b""

    async def write_file(self, path: str, content: bytes) -> None:
        return None

    async def upload(self, local: Path, remote: str) -> None:
        self.uploads.append((local, remote))

    async def download(self, remote: str, local: Path) -> None:
        return None

    async def start_service(self, name: str) -> CommandResult:
        return await self.execute(f"start {name}")

    async def stop_service(self, name: str) -> CommandResult:
        return await self.execute(f"stop {name}")

    async def inspect(self) -> dict[str, Any]:
        return {"docker": {"success": True, "output": "27"}}

    async def checkpoint(self, name: str) -> str:
        return self.current

    async def cleanup(self) -> None:
        return None


@pytest.mark.asyncio
async def test_compose_deployment_uploads_verifies_and_promotes(
    project: tuple[Path, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings, database = project
    (root / "compose.yaml").write_text(
        "services:\n  app:\n    image: example/app\n", encoding="utf-8"
    )
    settings.deployment.targets["production"] = DeploymentTargetConfig(
        type="ssh",
        host="example.invalid",
        username="deployer",
        deployment_path="/opt/apps/example",
        environment="production",
    )
    runtime = FakeDeploymentRuntime(current="/opt/apps/example/releases/old")
    manager = DeploymentManager(root, settings, database)
    monkeypatch.setattr(manager, "runtime", lambda target: runtime)
    result = await manager.apply("production", approved=True)
    assert result.status == "healthy"
    assert runtime.uploads
    assert any("docker compose" in item for item in runtime.commands)
    assert any("ln -sfn" in item for item in runtime.commands)


class BrokenDeploymentRuntime(FakeDeploymentRuntime):
    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        result = await super().execute(command, timeout=timeout, approved=approved)
        if "ps --format json" in command:
            return result.model_copy(update={"stdout": '{"State":"restarting"}'})
        return result


@pytest.mark.asyncio
async def test_broken_release_triggers_previous_release_rollback(
    project: tuple[Path, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings, database = project
    (root / "compose.yaml").write_text(
        "services:\n  app:\n    image: broken/app\n", encoding="utf-8"
    )
    settings.deployment.targets["production"] = DeploymentTargetConfig(
        type="ssh",
        host="example.invalid",
        username="deployer",
        deployment_path="/opt/apps/example",
        environment="production",
    )
    previous = "/opt/apps/example/releases/healthy"
    runtime = BrokenDeploymentRuntime(current=previous)
    manager = DeploymentManager(root, settings, database)
    monkeypatch.setattr(manager, "runtime", lambda target: runtime)
    with pytest.raises(DeploymentError, match="rollback"):
        await manager.apply("production", approved=True)
    assert any(
        f"docker compose -f {previous}" in item and "up -d" in item for item in runtime.commands
    )
    assert not any("ln -sfn" in item and "release-" in item for item in runtime.commands)
