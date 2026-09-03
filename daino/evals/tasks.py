"""End-to-end task evals: does this model actually finish the job.

The expensive kind. Each case gets a scratch Git repository built from its
fixture files, a real Daino project on top of it, and one real chat turn against
a real model. What is measured is the working tree afterwards — not what the
agent said it did.

That distinction is the whole reason this exists. An agent's summary is the least
reliable artefact it produces: a run that edits nothing and reports "done" reads
identically to one that worked. Assertions here are about files and exit codes.

Isolation is total and deliberate: a scratch directory, its own database, its own
``.daino`` state. A case must not see the developer's global configuration or
leave anything behind, or the second run of a suite measures something different
from the first.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Awaitable
from pathlib import Path
from time import monotonic

from daino.application.context import ProjectContext, initialize_project
from daino.application.mission_service import MissionApplicationService
from daino.config import load_settings, save_settings
from daino.config.models import Settings
from daino.evals.models import CaseResult, EvalCase, TaskExpectation, matches
from daino.schemas import ChatOutcome


class ScratchProject:
    """A throwaway Git repository with Daino initialised on top of it."""

    def __init__(self, case: EvalCase, settings: Settings) -> None:
        self.case = case
        self.settings = settings
        self.root = Path(tempfile.mkdtemp(prefix=f"daino-eval-{case.id}-"))
        self.context: ProjectContext | None = None

    def __enter__(self) -> ScratchProject:
        for relative, content in self.case.files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git("init", "-b", "main")
        self._git("config", "user.email", "eval@daino.local")
        self._git("config", "user.name", "Daino Eval")
        self._git("add", "-A")
        self._git("commit", "-m", "fixture", "--no-gpg-sign")
        initialize_project(self.root)
        # The suite's providers and routing, but the case's own runtime and
        # verification policy: an eval must not be able to reach the developer's
        # Docker daemon or run their project's test suite.
        project_settings = load_settings(self.root)
        project_settings.providers = dict(self.settings.providers)
        project_settings.models = dict(self.settings.models)
        project_settings.routing = dict(self.settings.routing)
        project_settings.routing_fallbacks = dict(self.settings.routing_fallbacks)
        project_settings.runtime.default = "sandbox"
        project_settings.verification.require_review = False
        project_settings.memory.auto_extract = False
        save_settings(project_settings, self.root)
        self.context = _open(self.root)
        return self

    def __exit__(self, *_: object) -> None:
        if self.context is not None:
            with contextlib.suppress(Exception):
                self.context.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _git(self, *arguments: str) -> None:
        subprocess.run(  # nosec B603, B607
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            check=False,
        )

    def changed_paths(self) -> set[str]:
        """Everything the run altered, from Git rather than from the agent.

        Asking Git is the point. An agent's own report of what it changed is a
        claim; the index is evidence.
        """
        result = subprocess.run(  # nosec B603, B607
            ["git", "status", "--porcelain"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        changed: set[str] = set()
        for line in result.stdout.splitlines():
            entry = line[3:].strip()
            if not entry or entry.startswith((".daino/", ".vasuki/")):
                continue
            # A rename is reported as "old -> new"; the new path is the one an
            # assertion is about.
            changed.add(entry.split(" -> ")[-1])
        return changed

    def run_command(self, command: str, timeout: float) -> tuple[int, str]:
        parts = command.split()
        try:
            result = subprocess.run(  # nosec B603
                parts,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, str(exc)
        return result.returncode, (result.stdout + result.stderr)[-4_000:]


def _open(root: Path) -> ProjectContext:
    """Open a scratch project without touching the user's global configuration."""
    from daino.application.context import open_project

    previous = os.environ.get("DAINO_CONFIG_HOME")
    scratch = root / ".daino" / "global"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["DAINO_CONFIG_HOME"] = str(scratch)
    try:
        return open_project(root)
    finally:
        if previous is None:
            os.environ.pop("DAINO_CONFIG_HOME", None)
        else:
            os.environ["DAINO_CONFIG_HOME"] = previous


async def run_task_case(
    case: EvalCase, settings: Settings, *, profile: str = ""
) -> CaseResult:
    """Run one case end to end and grade it on the working tree afterwards."""
    started = monotonic()
    with ScratchProject(case, settings) as project:
        if project.context is None:  # pragma: no cover - __enter__ always sets it
            raise RuntimeError("the scratch project did not open")
        service = MissionApplicationService(project.context)
        session = service.create_session(f"eval {case.id}")
        try:
            outcome = await _bounded(
                service.chat(case.instruction, session, profile_override=profile),
                case.timeout_seconds,
            )
        except TimeoutError:
            return CaseResult(
                case_id=case.id,
                kind=case.kind,
                passed=False,
                failures=[f"the run did not finish within {case.timeout_seconds:g}s"],
                duration_seconds=monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure is not a score
            return CaseResult(
                case_id=case.id,
                kind=case.kind,
                passed=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=monotonic() - started,
            )
        finally:
            with contextlib.suppress(Exception):
                await service.close_mcp()
            with contextlib.suppress(Exception):
                await service.close_code_intel()

        spend = service.core.gateway.budget_snapshot(outcome.mission_id)
        failures = _grade(case, project, outcome)
        return CaseResult(
            case_id=case.id,
            kind=case.kind,
            passed=not failures,
            failures=failures,
            duration_seconds=monotonic() - started,
            steps=outcome.steps,
            model_calls=spend.model_calls if spend else 0,
            total_tokens=spend.total_tokens if spend else 0,
            cost_usd=spend.cost_usd if spend else 0.0,
        )


async def _bounded(coroutine: Awaitable[ChatOutcome], timeout: float) -> ChatOutcome:
    return await asyncio.wait_for(coroutine, timeout=timeout)


def _grade(case: EvalCase, project: ScratchProject, outcome: object) -> list[str]:
    """Every assertion that did not hold, phrased so the number is actionable."""
    expectation = case.expect or TaskExpectation()
    failures: list[str] = []
    changed = project.changed_paths()
    for path in expectation.changed:
        if path not in changed:
            failures.append(
                f"{path} was not changed; the run touched {sorted(changed) or 'nothing'}"
            )
    for path in expectation.unchanged:
        if path in changed:
            failures.append(f"{path} was changed and should not have been")
    for path, pattern in expectation.contains.items():
        text = _read(project.root / path)
        if text is None:
            failures.append(f"{path} does not exist, so {pattern!r} cannot match")
        elif not matches(pattern, text):
            failures.append(f"{path} does not match {pattern!r}")
    for path, pattern in expectation.absent.items():
        text = _read(project.root / path)
        if text is not None and matches(pattern, text):
            failures.append(f"{path} still matches {pattern!r}")
    for command in expectation.commands:
        code, output = project.run_command(command, timeout=case.timeout_seconds)
        if code != 0:
            failures.append(f"`{command}` exited {code}:\n{output.strip()[-800:]}")
    if expectation.answer_matches:
        answer = getattr(outcome, "answer", "") or getattr(outcome, "summary", "")
        if not matches(expectation.answer_matches, answer):
            failures.append(
                f"the answer did not match {expectation.answer_matches!r}: {answer[:300]!r}"
            )
    steps = int(getattr(outcome, "steps", 0) or 0)
    if expectation.max_steps and steps > expectation.max_steps:
        failures.append(
            f"took {steps} steps, more than the {expectation.max_steps} expected"
        )
    return failures


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


__all__ = ["ScratchProject", "run_task_case"]
