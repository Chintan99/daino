"""Load suites, run them, and report what happened.

A suite is a YAML file. Built-in ones ship under ``daino/evals/suites`` and a
project can add its own under ``.daino/evals`` — a project's real tasks are
better evidence about a model than anything shipped, and the point of an eval
harness is that the numbers are about *your* work.

The report separates three outcomes rather than two. A case that failed is a
capability measurement; a case that errored — a provider outage, a missing
executable — is not, and folding the second into the first is how a benchmark
starts producing numbers that look like model quality and are actually network
weather.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from pydantic import ValidationError

from daino.config import paths
from daino.config.models import Settings
from daino.evals.models import CaseResult, EvalCase, EvalSuite, SuiteResult
from daino.evals.offline import run_retrieval_case, run_sizing_case

#: Where built-in suites live.
BUILTIN_SUITES = Path(__file__).parent / "suites"
#: Where a project's own suites live. Inside the state directory, so nothing the
#: agent runs can edit the yardstick it is being measured with.
PROJECT_SUITES = "evals"


def suite_directories(root: Path | None = None) -> list[Path]:
    directories = [BUILTIN_SUITES]
    if root is not None:
        directories.append(paths.state_path(root, PROJECT_SUITES))
    return directories


def load_suites(root: Path | None = None) -> tuple[list[EvalSuite], list[str]]:
    """Every suite that loads, and a problem line for every one that does not."""
    suites: list[EvalSuite] = []
    problems: list[str] = []
    seen: set[str] = set()
    for directory in suite_directories(root):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            suite, problem = _load_suite(path)
            if problem:
                problems.append(problem)
                continue
            if suite is None:
                continue
            if suite.name in seen:
                # A project suite of the same name replaces the built-in one,
                # because project directories are appended last.
                suites = [item for item in suites if item.name != suite.name]
            seen.add(suite.name)
            suites.append(suite)
    return suites, problems


def _load_suite(path: Path) -> tuple[EvalSuite | None, str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, f"{path}: could not be read ({exc})"
    if not isinstance(raw, dict):
        return None, f"{path}: expected a mapping with 'name' and 'cases'"
    raw.setdefault("name", path.stem)
    try:
        return EvalSuite.model_validate(raw), ""
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        return None, f"{path}: {location}: {error['msg']}"


def select_cases(
    suites: list[EvalSuite],
    *,
    suite_names: list[str] | None = None,
    kinds: list[str] | None = None,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
) -> list[tuple[EvalSuite, EvalCase]]:
    """Filter down to the cases a run should actually execute."""
    chosen: list[tuple[EvalSuite, EvalCase]] = []
    for suite in suites:
        if suite_names and suite.name not in suite_names:
            continue
        for case in suite.cases:
            if kinds and case.kind not in kinds:
                continue
            if case_ids and case.id not in case_ids:
                continue
            if tags and not set(tags) & set(case.tags):
                continue
            chosen.append((suite, case))
    return chosen


async def run_cases(
    selected: list[tuple[EvalSuite, EvalCase]],
    *,
    settings: Settings | None = None,
    profile: str = "",
    on_result: object = None,
) -> list[SuiteResult]:
    """Run every selected case, grouped into one result per suite.

    Sequential rather than concurrent. Task cases each drive a real agent
    against a real model, and running four at once against one provider produces
    rate limits and timings that say nothing about the agent.
    """
    grouped: dict[str, SuiteResult] = {}
    for suite, case in selected:
        result = grouped.setdefault(suite.name, SuiteResult(suite=suite.name, model=profile))
        outcome = await run_case(case, settings=settings, profile=profile)
        result.results.append(outcome)
        if callable(on_result):
            on_result(suite, case, outcome)
    return list(grouped.values())


async def run_case(
    case: EvalCase, *, settings: Settings | None = None, profile: str = ""
) -> CaseResult:
    """Run one case of whichever kind it is."""
    if case.kind == "retrieval":
        return run_retrieval_case(case)
    if case.kind == "sizing":
        return run_sizing_case(case)
    if settings is None:
        return CaseResult(
            case_id=case.id,
            kind=case.kind,
            passed=False,
            error="a task case needs a configured model; none was supplied",
        )
    # Imported here so the offline kinds stay importable in an environment with
    # no project open and no provider configured.
    from daino.evals.tasks import run_task_case

    return await run_task_case(case, settings, profile=profile)


def run_offline(root: Path | None = None) -> list[SuiteResult]:
    """Run every model-free case. The form CI uses."""
    suites, _ = load_suites(root)
    selected = select_cases(suites, kinds=["retrieval", "sizing"])
    return asyncio.run(run_cases(selected))


def render_report(results: list[SuiteResult]) -> str:
    """A plain-text summary: the score, then every failure with its reason."""
    if not results:
        return "No eval cases matched."
    lines: list[str] = []
    total_passed = total_ran = total_errored = 0
    total_cost = 0.0
    total_tokens = 0
    for suite in results:
        header = f"{suite.suite}"
        if suite.model:
            header += f" [{suite.model}]"
        ran = suite.total - suite.errored
        lines.append(
            f"{header}: {suite.passed}/{ran} passed"
            + (f" ({suite.success_rate:.0%})" if ran else "")
            + (f", {suite.errored} could not run" if suite.errored else "")
        )
        total_passed += suite.passed
        total_ran += ran
        total_errored += suite.errored
        total_cost += suite.total_cost_usd
        total_tokens += suite.total_tokens
        for case in suite.results:
            if case.passed:
                continue
            if case.skipped:
                lines.append(f"  ~ {case.case_id}: could not run — {case.error}")
                continue
            lines.append(f"  ✗ {case.case_id}")
            lines.extend(f"      {failure}" for failure in case.failures)
    if len(results) > 1 or total_errored:
        lines.append("")
        lines.append(
            f"Total: {total_passed}/{total_ran} passed"
            + (f" ({total_passed / total_ran:.0%})" if total_ran else "")
            + (f", {total_errored} could not run" if total_errored else "")
        )
    if total_tokens or total_cost:
        lines.append(
            f"Spent: {total_tokens:,} tokens" + (f", ${total_cost:.4f}" if total_cost else "")
        )
    return "\n".join(lines)
