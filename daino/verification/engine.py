"""Verification gates and compact failure classification."""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from daino.runtimes.base import Runtime
from daino.schemas.core import (
    CommandResult,
    FailureReport,
    VerificationCheck,
    VerificationReport,
)
from daino.security.policy import SHELL_OPERATORS

TRACE_LOCATION = re.compile(r'File "([^"]+)", line (\d+)')

#: A shell reporting that the program it was asked to run does not exist.
#: Covers dash ("sh: 1: git: not found") and bash ("bash: node: command not
#: found"), with or without a leading shell name.
MISSING_EXECUTABLE = re.compile(
    r"(?:^|\n)\s*(?:[\w./-]*(?:sh|zsh|dash): )?(?:line )?(?:\d+: )?"
    r"([\w.+-]+): (?:command )?not found",
    re.IGNORECASE,
)


def missing_executable(command: str, output: str) -> str:
    """Name the program a check needed and the runtime did not have.

    A check that never ran because ``git`` or ``node`` is absent from the
    runtime says nothing about the code it was meant to check. Distinguishing
    that from a real failure keeps a sound edit from being reported as broken:
    the field case was ``git diff --check`` inside a ``python:3.12-slim``
    container, which has no Git, failing a finished mission.
    """
    for candidate in MISSING_EXECUTABLE.findall(output or ""):
        # Only the program the check itself invokes counts. A test that fails
        # because the code under test cannot find something is a real failure.
        if command and candidate in shlex.split(command, posix=True)[:1]:
            return candidate
    return ""


class VerificationEngine:
    def __init__(
        self,
        root: Path,
        runtime: Runtime,
        *,
        execute: Callable[[str], Awaitable[CommandResult]] | None = None,
    ) -> None:
        self.root = root
        self.runtime = runtime
        self.execute = execute or runtime.execute

    @staticmethod
    def runnable(command: str) -> bool:
        """Report whether a command can execute without a shell.

        Planners keep proposing one-liners like ``grep -c foo page.html | head``.
        Those cannot run here, and letting one reach the runtime aborts a mission
        whose code changes were fine.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        return bool(tokens) and not any(token in SHELL_OPERATORS for token in tokens)

    def discover_commands(self) -> list[str]:
        commands: list[str] = []
        if (self.root / "pyproject.toml").exists() or (self.root / "setup.cfg").exists():
            # Never the bare name "python": it does not exist on a modern macOS
            # or most Linux distributions, where the binary is python3. Using it
            # made the syntax check report a failure on every such machine, for
            # a project whose syntax was fine.
            commands.append(f"{shlex.quote(self._python())} -m compileall -q .")
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

    def resolve_interpreter(self, command: str) -> str:
        """Rewrite a bare ``python`` command to an interpreter that exists.

        ``discover_commands`` has never used the bare name, but a *model* writes
        verification commands too, and it writes what it has read a thousand
        times: ``python test_app.py``. That program does not exist on a modern
        macOS or most Linux distributions, where the binary is ``python3``.

        The mission then dies on "Executable not found: python" — reported as a
        verification failure, so the user is told their finished, correct change
        did not pass its tests. This is the last entry point where a bare
        ``python`` could still reach the runtime; the same bug was fixed in the
        QA service and in ``discover_commands`` already.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command
        if not tokens or tokens[0] != "python":
            return command
        return shlex.join([self._python(), *tokens[1:]])

    def _python(self) -> str:
        """The interpreter to verify with: the project's own, else Daino's."""
        for relative in (Path(".venv/bin/python"), Path(".venv/Scripts/python.exe")):
            if (self.root / relative).is_file():
                return str(self.root / relative)
        return sys.executable or "python3"

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
        requested = [
            self.resolve_interpreter(item) for item in (commands or self.discover_commands())
        ]
        usable = [command for command in requested if self.runnable(command)]
        skipped = [command for command in requested if not self.runnable(command)]
        if not usable:
            # Everything proposed needs a shell; fall back to what the repository
            # itself supports rather than failing the mission on the checks.
            usable = [command for command in self.discover_commands() if self.runnable(command)]
        for command in skipped:
            checks.append(
                VerificationCheck(
                    name="skipped",
                    command=command,
                    passed=True,
                    skipped=True,
                    skip_reason="needs a shell; verification commands run directly",
                )
            )
        for command in usable:
            result = await self.execute(command)
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
