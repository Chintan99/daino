"""Run shell commands for the agent, through policy and the project runtime.

An agent that cannot run anything is a text editor that guesses. This is the
piece that lets it install a missing dependency, run the tests, and read the
failure — which is what turns a single edit into a loop that converges.

Nothing here decides policy or executes directly: ``CommandGate`` decides, the
``Runtime`` executes (locally, in Docker, or over SSH depending on project
configuration), and this joins them and asks the user when the gate says to.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from vasuki.runtimes.base import Runtime
from vasuki.schemas import ToolResult
from vasuki.security.commands import CommandGate, Verdict

#: Answered by the interface when the gate says a command needs a decision.
#: Returns (approved, remember_for_session).
ApprovalCallback = Callable[[str, str], Awaitable[tuple[bool, bool]]]

#: Command output kept in the observation. Enough to see a traceback or a test
#: summary; bounded so one noisy build cannot consume the whole context.
MAX_OUTPUT_CHARS = 8_000

DEFAULT_TIMEOUT_SECONDS = 180


class CommandRunner:
    """Policy-gated command execution for one agent session."""

    def __init__(
        self,
        runtime: Runtime,
        gate: CommandGate,
        *,
        runtime_name: str = "local",
        approve: ApprovalCallback | None = None,
        default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
        unavailable: str = "",
    ) -> None:
        self.runtime = runtime
        self.gate = gate
        self.runtime_name = runtime_name
        self.approve = approve
        self.default_timeout = default_timeout
        #: Set when the runtime could not start, for example Docker is configured
        #: but not installed. The agent gets one clear explanation rather than a
        #: fresh stack trace on every command it tries.
        self.unavailable = unavailable

    async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
        command = command.strip()
        if not command:
            return ToolResult(tool="run_command", success=False, error="No command given.")
        if self.unavailable:
            return ToolResult(tool="run_command", success=False, error=self.unavailable)

        decision = self.gate.decide(command, runtime=self.runtime_name)
        if decision.verdict is Verdict.DENY:
            return ToolResult(
                tool="run_command",
                success=False,
                error=(
                    f"Refused: {decision.reason}. This command cannot be approved; "
                    "achieve the goal a different way."
                ),
            )
        if decision.verdict is Verdict.ASK:
            if self.approve is None:
                return ToolResult(
                    tool="run_command",
                    success=False,
                    error=(
                        f"Needs approval ({decision.reason}) and no approver is attached, "
                        "so it was not run."
                    ),
                )
            approved, remember = await self.approve(command, decision.reason)
            if not approved:
                return ToolResult(
                    tool="run_command",
                    success=False,
                    error=f"The user declined to run: {command}",
                )
            if remember:
                self.gate.remember(decision.signature)

        try:
            result = await self.runtime.execute(
                command,
                timeout=timeout or self.default_timeout,
                # The gate has already decided; the runtime's own policy check
                # must not ask a second time for something just approved.
                approved=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failed command is an observation
            return ToolResult(
                tool="run_command", success=False, error=f"{type(exc).__name__}: {exc}"
            )

        stdout = _clip(result.stdout)
        stderr = _clip(result.stderr)
        error = stderr or stdout
        if not result.succeeded and self.runtime_name == "docker" and _looks_missing(error):
            error = (
                f"{error}\nThis command ran inside the configured Docker sandbox image. "
                "That image may not contain this executable. For a Compose project, run a "
                "docker compose command so Vasuki can use the host Docker daemon, or switch "
                "to /runtime local."
            )
        return ToolResult(
            tool="run_command",
            success=result.succeeded,
            data={
                "command": command,
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "runtime": self.runtime_name,
            },
            error=(
                None
                if result.succeeded
                else (
                    error
                    # No output at all: say what actually happened rather than
                    # "command failed", which names neither cause nor remedy.
                    or (
                        f"timed out after {timeout or self.default_timeout}s"
                        if result.timed_out
                        else f"exited with status {result.exit_code} and produced no output "
                        f"(runtime: {self.runtime_name})"
                    )
                )
            ),
            duration_seconds=result.duration_seconds,
        )


def _clip(text: str) -> str:
    """Keep the head and tail of long output; the middle is rarely the failure."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:half]}\n… {dropped} characters trimmed …\n{text[-half:]}"


def _looks_missing(output: str) -> bool:
    lowered = output.lower()
    markers = ("not found", "no such file", "executable not found")
    return any(marker in lowered for marker in markers)
