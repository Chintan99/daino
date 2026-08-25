"""Ephemeral Docker sandbox runtime."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from vasuki.runtimes.local import LocalRuntime
from vasuki.schemas import CommandResult
from vasuki.security import PolicyEngine


class DockerRuntime(LocalRuntime):
    def __init__(
        self,
        root: Path,
        *,
        image: str = "python:3.12",
        cpu_limit: float = 2,
        memory_limit: str = "2g",
        network_access: bool = False,
        timeout: int = 600,
        policy: PolicyEngine | None = None,
    ) -> None:
        super().__init__(root, policy=policy, timeout=timeout)
        self.image = image
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.network_access = network_access

    async def prepare(self) -> None:
        await super().prepare()
        if not shutil.which("docker"):
            raise RuntimeError("Docker is not installed or not on PATH")
        check = await super().execute("docker version", approved=True)
        if not check.succeeded:
            raise RuntimeError(f"Docker is unavailable: {check.stderr}")

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        decision = self.policy.command_decision(command, runtime="docker", approved=approved)
        if not decision.allowed:
            from vasuki.exceptions import PolicyDenied

            raise PolicyDenied("; ".join(decision.reasons))
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = []
        # The Docker CLI controls the host daemon. Running it inside the generic
        # sandbox image causes a misleading `docker: not found`, and mounting the
        # daemon socket into an untrusted build container would hand that
        # container root-equivalent host access. Keep the policy decision above,
        # then invoke the host client directly.
        if arguments and arguments[0].rsplit("/", 1)[-1] == "docker":
            return await super().execute(command, timeout=timeout, approved=True)

        network = "bridge" if self.network_access else "none"
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        user = f"--user {getuid()}:{getgid()} " if callable(getuid) and callable(getgid) else ""
        docker_command = (
            f"docker run --rm --cpus {self.cpu_limit} --memory {shlex.quote(self.memory_limit)} "
            f"--network {network} {user}-e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 "
            f"-e PYTHONPATH=/workspace/src "
            f"-v {shlex.quote(str(self.root))}:/workspace:rw "
            f"-w /workspace {shlex.quote(self.image)} sh -lc {shlex.quote(command)}"
        )
        return await super().execute(docker_command, timeout=timeout, approved=True)

    async def inspect(self) -> dict[str, Any]:
        version = await super().execute(
            "docker version --format {{.Server.Version}}", approved=True
        )
        return {
            "type": "docker",
            "available": version.succeeded,
            "version": version.stdout.strip(),
            "image": self.image,
            "network": "bridge" if self.network_access else "none",
            "cpu_limit": self.cpu_limit,
            "memory_limit": self.memory_limit,
        }
