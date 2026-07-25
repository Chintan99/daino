"""Verification gates and compact failure classification."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from vasuki.runtimes.base import Runtime
from vasuki.schemas.core import (
    FailureReport,
    VerificationCheck,
    VerificationReport,
)

TRACE_LOCATION = re.compile(r'File "([^"]+)", line (\d+)')


class VerificationEngine:
    def __init__(self, root: Path, runtime: Runtime) -> None:
        self.root = root
        self.runtime = runtime

    def discover_commands(self) -> list[str]:
        commands: list[str] = []
        if (self.root / "pyproject.toml").exists() or (self.root / "setup.cfg").exists():
            commands.append("python -m compileall -q .")
            if (self.root / "tests").exists():
                commands.append("pytest")
        if (self.root / "package.json").exists():
            commands.extend(["npm test", "npm run build"])
        if (self.root / "Cargo.toml").exists():
            commands.append("cargo test")
        if (self.root / "go.mod").exists():
            commands.append("go test ./...")
        if not commands:
            commands.append("git diff --check")
        return commands

    @staticmethod
    def summarize_failure(command: str, stdout: str, stderr: str) -> FailureReport:
        output = (stderr or stdout).strip()
        location = TRACE_LOCATION.search(output)
        lower = output.lower()
        failure_type = "Command failure"
        correction = None
        if "syntaxerror" in lower:
            failure_type = "Syntax error"
            correction = "invalid source syntax"
        elif "assertionerror" in lower or "failed" in lower:
            failure_type = "Test failure"
            correction = "implementation or test expectation"
        elif "type" in lower and "error" in lower:
            failure_type = "Type mismatch"
            correction = "typed interface or missing narrowing"
        elif "not found" in lower or "no such file" in lower:
            failure_type = "Missing dependency or file"
            correction = "runtime prerequisites or path"
        return FailureReport(
            failure_type=failure_type,
            command=command,
            summary=output.splitlines()[-1][:500] if output else "Command exited non-zero",
            file=location.group(1) if location else None,
            line=int(location.group(2)) if location else None,
            likely_correction_area=correction,
            output_excerpt=output[-4000:],
        )

    async def run(self, commands: list[str] | None = None) -> VerificationReport:
        started = datetime.now(UTC)
        checks: list[VerificationCheck] = []
        failures: list[FailureReport] = []
        for command in commands or self.discover_commands():
            result = await self.runtime.execute(command)
            check = VerificationCheck(
                name=command.split()[0],
                command=command,
                passed=result.succeeded,
                result=result,
            )
            checks.append(check)
            if not result.succeeded:
                failures.append(self.summarize_failure(command, result.stdout, result.stderr))
                break
        return VerificationReport(
            passed=not failures,
            checks=checks,
            failures=failures,
            started_at=started,
            finished_at=datetime.now(UTC),
        )
