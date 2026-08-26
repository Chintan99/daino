"""Application facade for project verification."""

from __future__ import annotations

from time import monotonic

from daino.application.context import ProjectContext
from daino.events import TestsCompleted, TestsStarted
from daino.missions import MissionService
from daino.persistence.models import VerificationRun
from daino.schemas import CommandResult, FailureReport, VerificationReport
from daino.security.commands import CommandGate
from daino.tools.commands import ApprovalCallback, CommandRunner
from daino.utils.ids import new_id
from daino.verification import VerificationEngine


class VerificationApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    async def run(
        self,
        commands: list[str] | None = None,
        *,
        mission_id: str | None = None,
        approve: ApprovalCallback | None = None,
        gate: CommandGate | None = None,
    ) -> VerificationReport:
        core = MissionService(
            self.context.root,
            self.context.settings,
            self.context.database,
            events=self.context.events,
        )
        runtime = core._runtime(self.context.root)
        runner = CommandRunner(
            runtime,
            gate or CommandGate(self.context.settings.security),
            runtime_name=self.context.settings.runtime.default,
            approve=approve,
            default_timeout=self.context.settings.runtime.command_timeout_seconds,
        )

        async def execute(command: str) -> CommandResult:
            result = await runner.run(command)
            data = result.data
            return CommandResult(
                command=command,
                exit_code=int(data.get("exit_code", 0 if result.success else 1)),
                stdout=str(data.get("stdout", "")),
                stderr=str(data.get("stderr", "")) or (result.error or ""),
                duration_seconds=result.duration_seconds,
                timed_out=bool(data.get("timed_out", False)),
            )

        engine = VerificationEngine(self.context.root, runtime, execute=execute)
        selected = commands or engine.discover_commands()
        self.context.events.publish(TestsStarted(mission_id=mission_id, commands=selected))
        started = monotonic()
        report: VerificationReport | None = None
        error: BaseException | None = None
        try:
            await runtime.prepare()
            report = await engine.run(selected)
        except BaseException as exc:
            error = exc
        try:
            await runtime.cleanup()
        except BaseException as exc:
            # Cleanup is part of the verification lifecycle. If it fails after
            # otherwise successful checks, close the UI's running state and
            # report the lifecycle failure instead of leaving verification hung.
            if error is None:
                error = exc
        if error is not None:
            summary = str(error) or type(error).__name__
            failure = FailureReport(
                failure_type=type(error).__name__,
                command=selected[0] if selected else "<runtime>",
                summary=summary,
                output_excerpt=summary,
            )
            self.context.events.publish(
                TestsCompleted(
                    mission_id=mission_id,
                    passed=False,
                    failed_count=1,
                    duration_seconds=monotonic() - started,
                    failures=[failure.model_dump(mode="json")],
                )
            )
            raise error
        assert report is not None
        if mission_id:
            with self.context.database.session() as session:
                session.add(
                    VerificationRun(
                        id=new_id("verification"),
                        mission_id=mission_id,
                        task_id=None,
                        passed=report.passed,
                        report=report.model_dump(mode="json"),
                    )
                )
        duration = (report.finished_at - report.started_at).total_seconds()
        self.context.events.publish(
            TestsCompleted(
                mission_id=mission_id,
                passed=report.passed,
                passed_count=sum(check.passed for check in report.checks),
                failed_count=len(report.failures),
                duration_seconds=duration,
                failures=[item.model_dump(mode="json") for item in report.failures],
            )
        )
        return report
