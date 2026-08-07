"""Application facade for project verification."""

from __future__ import annotations

from vasuki.application.context import ProjectContext
from vasuki.events import TestsCompleted, TestsStarted
from vasuki.missions import MissionService
from vasuki.verification import VerificationEngine


class VerificationApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    async def run(self, commands: list[str] | None = None) -> object:
        core = MissionService(
            self.context.root,
            self.context.settings,
            self.context.database,
            events=self.context.events,
        )
        runtime = core._runtime(self.context.root)
        engine = VerificationEngine(self.context.root, runtime)
        selected = commands or engine.discover_commands()
        self.context.events.publish(TestsStarted(commands=selected))
        await runtime.prepare()
        try:
            report = await engine.run(selected)
        finally:
            await runtime.cleanup()
        duration = (report.finished_at - report.started_at).total_seconds()
        self.context.events.publish(
            TestsCompleted(
                passed=report.passed,
                passed_count=sum(check.passed for check in report.checks),
                failed_count=len(report.failures),
                duration_seconds=duration,
                failures=[item.model_dump(mode="json") for item in report.failures],
            )
        )
        return report
