"""Controlled local subprocess runtime."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

from vasuki.exceptions import PolicyDenied
from vasuki.runtimes.base import Runtime
from vasuki.schemas import CommandResult
from vasuki.security import PolicyEngine, redact


class LocalRuntime(Runtime):
    def __init__(
        self,
        root: Path,
        policy: PolicyEngine | None = None,
        *,
        timeout: int = 600,
        allow_absolute_paths: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy or PolicyEngine()
        self.default_timeout = timeout
        self.allow_absolute_paths = allow_absolute_paths

    def _file_path(self, path: str) -> Path:
        candidate = Path(path)
        target = (
            candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        )
        if not self.allow_absolute_paths and not target.is_relative_to(self.root):
            raise ValueError("Path escapes runtime root")
        return target

    async def prepare(self) -> None:
        if not self.root.exists():
            raise FileNotFoundError(self.root)

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        decision = self.policy.command_decision(command, runtime="local", approved=approved)
        if not decision.allowed:
            raise PolicyDenied("; ".join(decision.reasons))
        try:
            arguments = shlex.split(command)
        except ValueError as exc:
            raise PolicyDenied(f"Malformed command: {exc}") from exc
        if not arguments:
            raise PolicyDenied("Empty command")
        started = time.monotonic()
        environment = os.environ.copy()
        source_root = self.root / "src"
        if source_root.is_dir():
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(source_root), existing_pythonpath) if item
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=self.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # A missing test runner is verification evidence, not an internal
            # crash. Returning the conventional command-not-found status lets
            # the repair loop explain the prerequisite and fail cleanly.
            return CommandResult(
                command=command,
                exit_code=127,
                stdout="",
                stderr=f"Executable not found: {arguments[0]}",
                duration_seconds=time.monotonic() - started,
            )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.default_timeout
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        return CommandResult(
            command=command,
            exit_code=process.returncode if not timed_out else 124,
            stdout=redact(stdout.decode(errors="replace")),
            stderr=redact(stderr.decode(errors="replace")),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )

    async def read_file(self, path: str) -> bytes:
        return self._file_path(path).read_bytes()

    async def write_file(self, path: str, content: bytes) -> None:
        target = self._file_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    async def upload(self, local: Path, remote: str) -> None:
        destination = self._file_path(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, destination)

    async def download(self, remote: str, local: Path) -> None:
        source = self._file_path(remote)
        shutil.copy2(source, local)

    async def start_service(self, name: str) -> CommandResult:
        return await self.execute(f"systemctl start {shlex.quote(name)}", approved=True)

    async def stop_service(self, name: str) -> CommandResult:
        return await self.execute(f"systemctl stop {shlex.quote(name)}", approved=True)

    async def inspect(self) -> dict[str, Any]:
        return {
            "type": "local",
            "root": str(self.root),
            "executables": {
                name: shutil.which(name)
                for name in ("git", "docker", "python", "pytest", "ruff", "mypy")
            },
        }

    async def checkpoint(self, name: str) -> str:
        return name

    async def cleanup(self) -> None:
        return None
