"""The eval harness itself, plus the built-in offline suites as a regression gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from daino.evals import (
    EvalCase,
    RetrievalExpectation,
    SizingExpectation,
    load_suites,
    render_report,
    run_offline,
    select_cases,
    synthetic_index,
)
from daino.evals.models import CaseResult, SuiteResult
from daino.evals.offline import run_retrieval_case, run_sizing_case
from daino.evals.runner import BUILTIN_SUITES, run_cases


def test_the_builtin_offline_suites_all_pass() -> None:
    """The gate the suites exist to be.

    This is the point of the whole harness: the ranking constants and the sizing
    thresholds were hand-tuned against real failures, and until now the only way
    to discover a change had broken one was to watch an agent behave worse a week
    later. A regression in either fails here instead.
    """
    results = run_offline()
    assert results, "no offline suites were found"
    for suite in results:
        assert suite.errored == 0, f"{suite.suite}: {[r.error for r in suite.results]}"
        failures = [
            f"{item.case_id}: {'; '.join(item.failures)}"
            for item in suite.results
            if not item.passed
        ]
        assert not failures, f"{suite.suite} regressed:\n" + "\n".join(failures)


def test_every_shipped_suite_parses() -> None:
    suites, problems = load_suites()
    assert not problems
    assert {suite.name for suite in suites} == {"retrieval", "sizing", "tasks"}


def test_task_cases_are_excluded_from_an_offline_run() -> None:
    """They cost money and need a model; a default run must never start one."""
    suites, _ = load_suites()
    offline = select_cases(suites, kinds=["retrieval", "sizing"])
    assert offline
    assert all(not case.needs_a_model for _, case in offline)


def test_a_case_must_carry_the_block_its_kind_needs() -> None:
    with pytest.raises(ValidationError, match="needs a 'retrieval' block"):
        EvalCase(id="c", kind="retrieval")
    with pytest.raises(ValidationError, match="needs a 'sizing' block"):
        EvalCase(id="c", kind="sizing")
    with pytest.raises(ValidationError, match="needs an 'expect' block"):
        EvalCase(id="c", kind="task", instruction="do it")


def test_the_synthetic_index_resolves_imports_between_fixture_files() -> None:
    index = synthetic_index(
        {
            "app/service.py": "from app.client import fetch\n\ndef go():\n    return fetch()\n",
            "app/client.py": "def fetch():\n    return 1\n",
            "notes.md": "prose",
        }
    )
    by_path = {item.path: item for item in index.files}
    assert by_path["app/service.py"].imports == ["app/client.py"]
    # An unresolvable import is third-party and is dropped, as the real graph does.
    assert by_path["app/client.py"].imports == []
    assert [symbol.name for symbol in by_path["app/client.py"].symbols] == ["fetch"]
    assert index.languages == {"python": 2, "markdown": 1}


def test_the_synthetic_index_is_identical_on_every_run() -> None:
    """A case's inputs must not vary, or a failure is not reproducible."""
    files = {"a.py": "def go():\n    return 1\n"}
    assert synthetic_index(files) == synthetic_index(files)


def test_an_order_rule_catches_an_inverted_ranking() -> None:
    case = EvalCase(
        id="order",
        kind="retrieval",
        instruction="change the client",
        required=["app/service.py"],
        files={
            "app/service.py": "from app.client import fetch\n",
            "app/client.py": "def fetch():\n    return 1\n",
            "unrelated/other.py": "def other():\n    return 2\n",
        },
        retrieval=RetrievalExpectation(order=["unrelated/other.py > app/client.py"]),
    )
    result = run_retrieval_case(case)
    assert not result.passed
    assert "inverted" in result.failures[0] or "not selected at all" in result.failures[0]


def test_a_failure_names_what_the_ranking_actually_chose() -> None:
    """A red eval that does not say what happened is a red eval nobody fixes."""
    case = EvalCase(
        id="missing",
        kind="retrieval",
        instruction="anything",
        files={"a.py": "def go():\n    return 1\n"},
        retrieval=RetrievalExpectation(includes=["nowhere.py"]),
    )
    result = run_retrieval_case(case)
    assert not result.passed
    assert "the ranking chose" in result.failures[0]


def test_a_sizing_bound_reports_the_number_it_saw() -> None:
    case = EvalCase(
        id="sizing",
        kind="sizing",
        profile={"provider": "p", "model": "m", "context_window": 8192, "local": True},
        sizing=SizingExpectation(compact=False),
    )
    result = run_sizing_case(case)
    assert not result.passed
    assert "compact is True, expected False" in result.failures[0]


def test_an_invalid_profile_is_an_error_not_a_failure() -> None:
    """A broken fixture is not a capability measurement."""
    case = EvalCase(
        id="bad",
        kind="sizing",
        profile={"model": "m"},
        sizing=SizingExpectation(compact=True),
    )
    result = run_sizing_case(case)
    assert result.skipped
    assert "profile is not valid" in result.error


def test_a_malformed_suite_is_reported_rather_than_raised(tmp_path: Path) -> None:
    directory = tmp_path / ".daino" / "evals"
    directory.mkdir(parents=True)
    (directory / "broken.yaml").write_text("cases: [{id: x, kind: nonsense}]", encoding="utf-8")
    suites, problems = load_suites(tmp_path)
    assert any("broken.yaml" in problem for problem in problems)
    # The built-in suites still loaded.
    assert {suite.name for suite in suites} >= {"retrieval", "sizing"}


def test_a_project_suite_replaces_a_builtin_of_the_same_name(tmp_path: Path) -> None:
    directory = tmp_path / ".daino" / "evals"
    directory.mkdir(parents=True)
    (directory / "retrieval.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "retrieval",
                "cases": [
                    {
                        "id": "ours",
                        "kind": "retrieval",
                        "files": {"a.py": "def go():\n    return 1\n"},
                        "instruction": "x",
                        "retrieval": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    suites, problems = load_suites(tmp_path)
    assert not problems
    retrieval = next(suite for suite in suites if suite.name == "retrieval")
    assert [case.id for case in retrieval.cases] == ["ours"]


def test_a_task_case_without_a_model_is_an_error_not_a_failure() -> None:
    suites, _ = load_suites()
    selected = select_cases(suites, kinds=["task"])
    assert selected
    results = asyncio.run(run_cases(selected[:1]))
    assert results[0].results[0].skipped
    assert "needs a configured model" in results[0].results[0].error


def test_errored_cases_are_not_counted_as_failures() -> None:
    """Folding a provider outage into the score is how a benchmark starts lying."""
    suite = SuiteResult(
        suite="s",
        results=[
            CaseResult(case_id="a", kind="task", passed=True),
            CaseResult(case_id="b", kind="task", passed=False),
            CaseResult(case_id="c", kind="task", passed=False, error="provider down"),
        ],
    )
    assert suite.passed == 1
    assert suite.failed == 1
    assert suite.errored == 1
    # One of two that ran, not one of three.
    assert suite.success_rate == 0.5


def test_the_report_distinguishes_a_failure_from_a_non_run() -> None:
    rendered = render_report(
        [
            SuiteResult(
                suite="s",
                results=[
                    CaseResult(case_id="a", kind="task", passed=False, failures=["x.py missing"]),
                    CaseResult(case_id="b", kind="task", passed=False, error="provider down"),
                ],
            )
        ]
    )
    assert "✗ a" in rendered
    assert "x.py missing" in rendered
    assert "~ b: could not run — provider down" in rendered


def test_task_cases_grade_the_tree_not_the_agent_s_account() -> None:
    """The shipped task suite must assert on files, not only on the summary."""
    raw = yaml.safe_load((BUILTIN_SUITES / "tasks.yaml").read_text(encoding="utf-8"))
    graded = [
        case
        for case in raw["cases"]
        if case["expect"].get("changed")
        or case["expect"].get("unchanged")
        or case["expect"].get("commands")
    ]
    assert len(graded) == len(raw["cases"])


def test_the_failing_test_case_forbids_editing_the_test() -> None:
    """The cheap way to make a test pass is to change the test; that must fail."""
    raw = yaml.safe_load((BUILTIN_SUITES / "tasks.yaml").read_text(encoding="utf-8"))
    case = next(item for item in raw["cases"] if item["id"] == "fix-a-failing-test")
    assert "test_calculator.py" in case["expect"]["unchanged"]
    assert "calculator.py" in case["expect"]["changed"]
