"""AsyncSSH runtime with host-key verification, SFTP, and redacted audit results."""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh

from daino.config.models import DeploymentTargetConfig
from daino.exceptions import PolicyDenied
from daino.runtimes.base import Runtime
from daino.schemas import CommandResult
from daino.security import PolicyEngine, redact


class RemoteSSHRuntime(Runtime):
    def __init__(
        self,
        target: DeploymentTargetConfig,
        policy: PolicyEngine | None = None,
        *,
        timeout: int = 600,
    ) -> None:
        if not target.host or not target.username:
            raise ValueError("SSH targets require host and username")
        self.target = target
        self.policy = policy or PolicyEngine()
        self.default_timeout = timeout
        self.connection: asyncssh.SSHClientConnection | None = None

    async def prepare(self) -> None:
        kwargs: dict[str, Any] = {
            "host": self.target.host,
            "port": self.target.port,
            "username": self.target.username,
        }
        if self.target.auth.key_path:
            kwargs["client_keys"] = [str(Path(self.target.auth.key_path).expanduser())]
        if self.target.auth.known_hosts:
            kwargs["known_hosts"] = str(Path(self.target.auth.known_hosts).expanduser())
        self.connection = await asyncssh.connect(**kwargs)

    def _connection(self) -> asyncssh.SSHClientConnection:
        if self.connection is None:
            raise RuntimeError("SSH runtime has not been prepared")
        return self.connection

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        decision = self.policy.command_decision(command, runtime="ssh", approved=approved)
        if not decision.allowed:
            raise PolicyDenied("; ".join(decision.reasons))
        started = time.monotonic()
        timed_out = False
        try:
            result = await asyncio.wait_for(
                self._connection().run(command, check=False),
                timeout=timeout or self.default_timeout,
            )
            exit_code = result.exit_status
            stdout = str(result.stdout)
            stderr = str(result.stderr)
        except TimeoutError:
            timed_out = True
            exit_code = 124
            stdout = ""
            stderr = "Remote command timed out"
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=redact(stdout),
            stderr=redact(stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )

    async def read_file(self, path: str) -> bytes:
        async with self._connection().start_sftp_client() as sftp:
            async with sftp.open(path, "rb") as handle:
                return await handle.read()

    async def write_file(self, path: str, content: bytes) -> None:
        async with self._connection().start_sftp_client() as sftp:
            async with sftp.open(path, "wb") as handle:
                await handle.write(content)

    async def upload(self, local: Path, remote: str) -> None:
        async with self._connection().start_sftp_client() as sftp:
            await sftp.put(str(local), remote, recurse=local.is_dir())

    async def download(self, remote: str, local: Path) -> None:
        async with self._connection().start_sftp_client() as sftp:
            await sftp.get(remote, str(local), recurse=True)

    async def start_service(self, name: str) -> CommandResult:
        return await self.execute(f"sudo systemctl start {shlex.quote(name)}", approved=True)

    async def stop_service(self, name: str) -> CommandResult:
        return await self.execute(f"sudo systemctl stop {shlex.quote(name)}", approved=True)

    async def inspect(self) -> dict[str, Any]:
        """Collect read-only host facts; individual failures do not hide other facts."""
        commands = {
            "os": "uname -a",
            "os_release": "cat /etc/os-release",
            "cpu": "nproc",
            "memory": "free -h",
            "disk": "df -h",
            "gpu": "nvidia-smi -L",
            "docker": "docker version --format '{{.Server.Version}}'",
            "compose": "docker compose version",
            "containers": "docker ps --format '{{json .}}'",
            "ports": "ss -lntup",
            "git": "git --version",
            "mounts": "findmnt",
            "target_permissions": f"ls -ld {shlex.quote(self.target.deployment_path)}",
            "current_release": (f"readlink -f {shlex.quote(self.target.deployment_path)}/current"),
            "reverse_proxy": "sh -lc 'command -v nginx || command -v caddy || command -v traefik'",
            "firewall": "sh -lc 'sudo -n ufw status || sudo -n nft list ruleset'",
            "services": "systemctl --no-pager --state=running --type=service",
        }

        async def collect(name: str, command: str) -> tuple[str, dict[str, Any]]:
            result = await self.execute(command, timeout=30, approved=True)
            return name, {
                "success": result.succeeded,
                "output": result.stdout.strip() or result.stderr.strip(),
            }

        return dict(await asyncio.gather(*(collect(*item) for item in commands.items())))

    async def checkpoint(self, name: str) -> str:
        base = PurePosixPath(self.target.deployment_path)
        result = await self.execute(
            f"readlink -f {shlex.quote(str(base / 'current'))}", approved=True
        )
        return result.stdout.strip() or name

    async def cleanup(self) -> None:
        if self.connection is not None:
            self.connection.close()
            await self.connection.wait_closed()
            self.connection = None
