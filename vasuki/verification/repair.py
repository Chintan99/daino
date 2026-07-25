"""Bounded repair loop with explicit escalation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from vasuki.schemas import FailureReport, VerificationReport
from vasuki.verification.engine import VerificationEngine

RepairCallback = Callable[[FailureReport, int, bool], Awaitable[bool]]


class RepairLoop:
    def __init__(
        self,
        engine: VerificationEngine,
        *,
        local_attempts: int = 2,
        total_attempts: int = 4,
    ) -> None:
        if total_attempts < 1 or local_attempts < 0 or local_attempts > total_attempts:
            raise ValueError("Invalid repair limits")
        self.engine = engine
        self.local_attempts = local_attempts
        self.total_attempts = total_attempts

    async def run(
        self, commands: list[str], repair: RepairCallback
    ) -> tuple[VerificationReport, int]:
        report = await self.engine.run(commands)
        attempt = 0
        while not report.passed and attempt < self.total_attempts:
            attempt += 1
            escalated = attempt > self.local_attempts
            changed = await repair(report.failures[0], attempt, escalated)
            if not changed:
                break
            report = await self.engine.run(commands)
        return report, attempt
