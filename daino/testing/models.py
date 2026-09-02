"""What a test run is, once it has been read back in a comparable shape.

Every framework reports differently — pytest to stdout and JUnit XML, vitest to
its own JSON, `go test` to a stream of JSON lines — and a Tests panel that
parses each one's prose is a panel that is subtly wrong for most of them. So
everything here is the *normalised* form: one status vocabulary, one place a
failure's file and line live, one shape for coverage.

The models are deliberately flat. A test result is a row in a list that gets
clicked; nesting it by module would make the common case (which of these
failed?) require expanding things.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

#: A test's outcome. "errored" is distinct from "failed" on purpose: a test
#: whose setup blew up did not test anything, and telling those apart is what
#: stops a broken fixture reading as a broken feature.
TestStatus = Literal["passed", "failed", "errored", "skipped", "xfailed", "xpassed"]

#: How a whole run ended.
RunStatus = Literal["pending", "running", "passed", "failed", "cancelled", "errored"]


class TestCase(BaseModel):
    """One test, discovered or executed."""

    #: The framework's own identifier, and the only thing that can re-run
    #: exactly this test: "tests/test_a.py::TestClass::test_b" for pytest,
    #: "src/a.test.ts > suite > name" for vitest.
    id: str
    name: str
    #: Dotted suite/class path, for grouping without re-parsing the id.
    suite: str = ""
    #: Repository-relative, when the framework says. Empty when it does not.
    file: str = ""
    #: One-based, as the editor shows it. 0 means "not reported".
    line: int = 0


class TestResult(TestCase):
    """One test, with what happened to it."""

    status: TestStatus = "passed"
    duration_seconds: float = 0.0
    #: The assertion, the traceback, the error — whatever the runner said.
    message: str = ""
    #: Where the failure actually occurred, which is often not where the test
    #: is defined. This is the location a "go to failure" click should use.
    failure_file: str = ""
    failure_line: int = 0

    @property
    def failed(self) -> bool:
        return self.status in {"failed", "errored"}


class FileCoverage(BaseModel):
    path: str
    covered: int = 0
    total: int = 0
    #: Lines with no coverage, so the editor can mark them.
    missing: list[int] = Field(default_factory=list)

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 0.0


class Coverage(BaseModel):
    """Coverage as the runner reported it — never inferred, never estimated."""

    #: Which tool produced this, so a reader knows what it measures.
    source: str = ""
    covered: int = 0
    total: int = 0
    files: list[FileCoverage] = Field(default_factory=list)

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 0.0


class TestRun(BaseModel):
    """One execution of a framework's tests."""

    id: str
    framework: str
    #: Exactly what was executed, so a result can be reproduced by hand.
    command: str = ""
    status: RunStatus = "pending"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    results: list[TestResult] = Field(default_factory=list)
    #: Raw output, kept because a runner that crashed before producing a report
    #: has nothing else to explain itself with.
    output: str = ""
    #: Set when the run itself failed rather than the tests — a missing runner,
    #: a collection error, a timeout.
    error: str = ""
    coverage: Coverage | None = None
    #: The selection this run was given, if it was a subset.
    selection: list[str] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        tally = dict.fromkeys(
            ("passed", "failed", "errored", "skipped", "xfailed", "xpassed"), 0
        )
        for item in self.results:
            tally[item.status] += 1
        return tally

    @property
    def failures(self) -> list[TestResult]:
        return [item for item in self.results if item.failed]


class DiscoveredFramework(BaseModel):
    """A test framework this project uses, and whether it can be run."""

    id: str
    label: str
    #: The command that would run everything.
    command: str
    available: bool = True
    #: Why it cannot run, when it cannot.
    detail: str = ""
    #: How many tests discovery found. -1 means discovery was not attempted.
    test_count: int = -1
    #: Whether coverage can be collected without extra installation.
    supports_coverage: bool = False
