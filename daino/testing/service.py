"""Running a project's tests, and keeping the result worth looking at.

What makes this different from "run a command and show its output": the run is
*structured*. Discovery lists what exists, a run reports per-test outcomes with
locations, and "re-run the failures" selects exactly the tests that failed using
the framework's own identifiers rather than a name-matching guess.

One run at a time, per project. Tests share a working tree, a database, and
frequently a port; two concurrent runs produce two sets of results neither of
which describes anything. The second caller is told a run is in progress rather
than being quietly queued behind one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from daino.config import paths
from daino.testing import frameworks as detection
from daino.testing.frameworks import REPORT_DIRNAME, Framework
from daino.testing.models import (
    DiscoveredFramework,
    TestCase,
    TestResult,
    TestRun,
)
from daino.testing.reports import (
    parse_coverage,
    parse_discovery,
    parse_go,
    parse_jest,
    parse_junit,
)
from daino.utils.ids import new_id

#: How long a run may take before it is killed. Long, because a real suite is
#: allowed to be slow; bounded, because a hung test must not hold the runner
#: forever with no way back.
RUN_TIMEOUT_SECONDS = 1_800.0
#: Discovery is cheap and should feel instant; something is wrong past this.
DISCOVERY_TIMEOUT_SECONDS = 120.0
#: How much raw output is kept with a run. Enough to explain a crash, bounded
#: so a chatty suite cannot make the report unloadable.
MAX_OUTPUT_CHARS = 200_000

RunUpdate = Callable[[TestRun], None]


class TestRunError(RuntimeError):
    """Raised when a run cannot be started."""


class TestService:
    """Discover, run, and re-run a project's tests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        #: The run in flight, if any. Held rather than returned so a reconnected
        #: browser can pick up a run it did not start.
        self.current: TestRun | None = None
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        #: The last finished run, so the panel has something after a reload.
        self.last: TestRun | None = None

    # --------------------------------------------------------------- reports

    @property
    def reports_dir(self) -> Path:
        """Where runners write their reports.

        Under the state directory, never the project: a test run that dirtied
        the working tree would make every run look like an uncommitted change,
        and would show up in the Inspector's own diff.
        """
        return paths.state_dir(self.root, create=True) / REPORT_DIRNAME

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------- discovery

    def frameworks(self) -> list[Framework]:
        return detection.detect(self.root)

    async def discover(
        self, framework_id: str = ""
    ) -> tuple[list[DiscoveredFramework], list[TestCase]]:
        """What frameworks are here, and what tests they contain.

        Discovery is run for the framework asked for, or the first available one
        when none is named. Frameworks that cannot run are still listed — with
        the reason — because "no tests found" and "the runner is not installed"
        need to look different.
        """
        available = self.frameworks()
        described: list[DiscoveredFramework] = []
        cases: list[TestCase] = []
        target = framework_id or next((item.id for item in available if item.argv), "")
        for framework in available:
            runnable = bool(framework.argv)
            entry = DiscoveredFramework(
                id=framework.id,
                label=framework.label,
                command=" ".join(framework.argv) if runnable else "",
                available=runnable,
                detail=framework.detail,
                # Both halves, not just the flags: `go test -cover` prints
                # a percentage to stdout and writes no report, so offering
                # a coverage toggle for it promises a number nothing can read.
                supports_coverage=bool(framework.coverage_argv and framework.coverage_report),
            )
            if runnable and framework.id == target and framework.discover_argv:
                found, error = await self._collect(framework)
                cases = found
                entry.test_count = len(found)
                if error and not found:
                    # Collection errors are the single most common reason a
                    # suite reports nothing, and saying "0 tests" instead of
                    # showing the import error wastes everyone's afternoon.
                    entry.detail = error
            described.append(entry)
        return described, cases

    async def _collect(self, framework: Framework) -> tuple[list[TestCase], str]:
        argv = detection.discovery_command(framework)
        if not argv:
            return [], ""
        code, output = await self._capture(argv, DISCOVERY_TIMEOUT_SECONDS)
        cases = parse_discovery(output, framework.discover_format, self.root)
        if cases:
            return cases, ""
        if code != 0:
            tail = "\n".join(output.strip().splitlines()[-12:])
            return [], tail or f"{framework.label} could not list its tests."
        return [], ""

    # -------------------------------------------------------------- execution

    async def start(
        self,
        *,
        framework_id: str = "",
        selection: list[str] | None = None,
        coverage: bool = False,
        on_update: RunUpdate | None = None,
    ) -> TestRun:
        """Begin a run and return immediately with its initial state."""
        if self.running:
            raise TestRunError("A test run is already in progress.")
        framework = (
            detection.by_id(self.root, framework_id)
            if framework_id
            else next((item for item in self.frameworks() if item.argv), None)
        )
        if framework is None:
            raise TestRunError(
                "No test framework was detected in this project."
                if not framework_id
                else f"Unknown test framework {framework_id!r}."
            )
        try:
            invocation = detection.build(
                framework,
                self.reports_dir,
                selection=selection,
                coverage=coverage,
            )
        except ValueError as exc:
            raise TestRunError(str(exc)) from exc

        run = TestRun(
            id=new_id("testrun"),
            framework=framework.id,
            command=invocation.command,
            status="running",
            started_at=datetime.now(UTC),
            selection=list(selection or []),
        )
        self.current = run
        if on_update:
            on_update(run)
        task = asyncio.create_task(self._execute(run, framework, invocation, on_update))
        # A task cancelled before its first step never runs its body, so its
        # own except-block cannot settle the run. Without this the panel would
        # show a run that started and never finished.
        task.add_done_callback(lambda _: self._ensure_settled(run, on_update))
        self._task = task
        return run

    def _ensure_settled(self, run: TestRun, on_update: RunUpdate | None) -> None:
        """Close out a run whose task ended without reaching its own handler."""
        if run.finished_at is not None:
            return
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        run.error = run.error or "Cancelled before the runner started."
        self._settle(run, on_update)

    async def _execute(
        self,
        run: TestRun,
        framework: Framework,
        invocation: detection.Invocation,
        on_update: RunUpdate | None,
    ) -> None:
        # A stale report from the previous run would be read as this one's if the
        # runner dies before writing, so it goes first.
        for path in (invocation.report_path, invocation.coverage_path):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)

        started = datetime.now(UTC)
        try:
            code, output = await self._capture(
                invocation.argv, RUN_TIMEOUT_SECONDS, environment=invocation.environment
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.finished_at = datetime.now(UTC)
            run.error = "Cancelled."
            self._settle(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001 - a run must report, never vanish
            run.status = "errored"
            run.finished_at = datetime.now(UTC)
            run.error = f"{type(exc).__name__}: {exc}"
            self._settle(run, on_update)
            return

        run.output = output[-MAX_OUTPUT_CHARS:]
        run.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        run.results = self._read_results(framework, invocation, output)
        if invocation.coverage_path is not None:
            run.coverage = parse_coverage(
                invocation.coverage_path, framework.coverage_report, self.root
            )

        failures = run.failures
        if run.results:
            run.status = "failed" if failures else "passed"
        else:
            # No per-test results. The exit code is all there is to go on, and
            # saying so beats reporting "0 tests passed" as a success.
            run.status = "passed" if code == 0 else "failed"
            if code != 0 and framework.report == "none":
                run.error = (
                    f"{framework.label} exited {code}. "
                    "This runner does not report per-test results."
                )
            elif code != 0:
                tail = "\n".join(output.strip().splitlines()[-15:])
                run.error = tail or f"{framework.label} exited {code}."
        run.finished_at = datetime.now(UTC)
        self._settle(run, on_update)

    def _read_results(
        self,
        framework: Framework,
        invocation: detection.Invocation,
        output: str,
    ) -> list[TestResult]:
        if framework.report == "go-json":
            return parse_go(output, self.root)
        path = invocation.report_path
        if path is None or not path.is_file():
            return []
        try:
            payload = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if framework.report == "jest-json":
            return parse_jest(payload, self.root)
        if framework.report == "junit":
            return parse_junit(payload, self.root)
        return []

    def _settle(self, run: TestRun, on_update: RunUpdate | None) -> None:
        self.last = run
        self.current = run
        if on_update:
            on_update(run)

    def cancel(self) -> bool:
        """Stop the run in flight. Returns whether there was one."""
        if not self.running:
            return False
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
        task = self._task
        if task is not None:
            task.cancel()
        return True

    def rerun_selection(self) -> list[str]:
        """The ids of the tests that failed last time.

        The framework's own identifiers, so a re-run selects exactly these
        rather than everything whose name happens to match.
        """
        source = self.last
        return [item.id for item in source.failures] if source else []

    # ---------------------------------------------------------------- process

    async def _capture(
        self,
        argv: list[str],
        timeout: float,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run a command, returning its exit code and merged output.

        stderr is merged into stdout rather than kept apart: test runners
        interleave the two, and a panel that separates them shows a failure
        whose message is in one stream and whose location is in the other.
        """
        env = {
            **os.environ,
            # Colour codes in a report are noise the parsers have to strip.
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "PY_COLORS": "0",
            "CI": "1",
            **(environment or {}),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.root),
                env=env,
            )
        except (OSError, ValueError) as exc:
            return 127, f"{argv[0]}: {exc}"
        self._process = process
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            return 124, f"Timed out after {timeout:.0f}s."
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        finally:
            self._process = None
        return process.returncode or 0, stdout.decode("utf-8", "replace")
