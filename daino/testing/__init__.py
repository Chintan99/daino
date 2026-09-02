"""Structured test discovery and execution for the CODE workspace.

The Tests panel is driven from here. Its guiding constraint: the panel must
never disagree with what the same command shows in a terminal, which is why
results come from the runners' own machine-readable reports rather than from
scraping their human-readable output.
"""

from daino.testing.models import (
    Coverage,
    DiscoveredFramework,
    FileCoverage,
    RunStatus,
    TestCase,
    TestResult,
    TestRun,
    TestStatus,
)
from daino.testing.service import TestRunError, TestService

__all__ = [
    "Coverage",
    "DiscoveredFramework",
    "FileCoverage",
    "RunStatus",
    "TestCase",
    "TestResult",
    "TestRun",
    "TestRunError",
    "TestService",
    "TestStatus",
]
