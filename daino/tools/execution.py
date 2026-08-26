"""High-level execution tools."""

from __future__ import annotations

from daino.runtimes.base import Runtime
from daino.schemas import ToolResult


class ExecutionTools:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def run_command(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> ToolResult:
        try:
            result = await self.runtime.execute(command, timeout=timeout, approved=approved)
            return ToolResult(
                tool="run_command",
                success=result.succeeded,
                data={"result": result.model_dump(mode="json")},
                error=result.stderr if not result.succeeded else None,
                duration_seconds=result.duration_seconds,
            )
        except Exception as exc:
            return ToolResult(tool="run_command", success=False, error=str(exc))

    async def run_tests(self, command: str = "pytest") -> ToolResult:
        result = await self.run_command(command)
        return result.model_copy(update={"tool": "run_tests"})

    async def run_lint(self, command: str = "ruff check .") -> ToolResult:
        result = await self.run_command(command)
        return result.model_copy(update={"tool": "run_lint"})

    async def run_typecheck(self, command: str = "mypy .") -> ToolResult:
        result = await self.run_command(command)
        return result.model_copy(update={"tool": "run_typecheck"})

    async def run_security_scan(self, command: str = "bandit -r .") -> ToolResult:
        result = await self.run_command(command)
        return result.model_copy(update={"tool": "run_security_scan"})

    async def run_build(self, command: str) -> ToolResult:
        result = await self.run_command(command)
        return result.model_copy(update={"tool": "run_build"})
