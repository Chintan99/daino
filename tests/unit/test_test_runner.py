"""Discovering and running a project's tests, and reading what came back.

Exercised against real pytest invocations in a temporary project rather than
against canned output, because the things that break here are the details of
what runners actually emit: pytest's zero-based JUnit line numbers, its dotted
classnames, the difference between a failure and a collection error.

The report parsers are also tested directly against fixture documents, so a
framework nobody has installed on this machine is still covered.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.testing import TestRunError, TestService
from daino.testing.frameworks import Framework, build, detect
from daino.testing.reports import (
    parse_coverage,
    parse_go,
    parse_jest,
    parse_junit,
)

SUITE = textwrap.dedent(
    """
    def helper(value):
        assert value > 0, "helper says no"


    def test_passes():
        assert 1 + 1 == 2


    def test_fails_inside_a_helper():
        helper(-1)


    def test_skipped():
        import pytest
        pytest.skip("not today")
    """
).strip()


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_things.py").write_text(SUITE + "\n", encoding="utf-8")
    yield tmp_path


# --------------------------------------------------------------- detection


def test_a_python_project_offers_pytest(project: Path) -> None:
    found = detect(project)

    pytest_framework = next(item for item in found if item.id == "pytest")
    assert pytest_framework.argv
    # Never the bare name "python": it does not exist on modern macOS, and a
    # runner that cannot start reports failures that are not there.
    assert pytest_framework.argv[0] != "python"
    assert pytest_framework.report == "junit"


def test_an_unavailable_runner_is_listed_with_the_reason(tmp_path: Path) -> None:
    """ "No tests found" and "the runner is missing" are different problems."""
    (tmp_path / "package.json").write_text(
        '{"name": "app", "devDependencies": {"vitest": "^1"}}', encoding="utf-8"
    )

    found = detect(tmp_path)

    vitest = next(item for item in found if item.id == "vitest")
    assert vitest.argv == ()
    assert "not installed" in vitest.detail


def test_a_selection_uses_each_frameworks_own_syntax(tmp_path: Path) -> None:
    """pytest takes node ids; a name-pattern runner takes one alternation."""
    reports = tmp_path / "reports"
    node_ids = ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]

    pytest_like = build(
        Framework(
            id="pytest",
            label="pytest",
            argv=("py", "-m", "pytest"),
            report_flag="--junit-xml={path}",
        ),
        reports,
        selection=node_ids,
    )
    pattern_like = build(
        Framework(
            id="vitest",
            label="Vitest",
            argv=("vitest", "run"),
            select_prefix="-t",
        ),
        reports,
        selection=["adds numbers", "subtracts numbers"],
    )

    assert pytest_like.argv[-2:] == node_ids
    assert pattern_like.argv[-2:] == ["-t", "adds numbers|subtracts numbers"]


def test_reports_are_written_outside_the_working_tree(project: Path) -> None:
    """A test run that dirtied the repo would make every run look like a change."""
    service = TestService(project)

    assert ".vasuki" in service.reports_dir.parts or ".daino" in service.reports_dir.parts
    assert service.reports_dir.name == "test-reports"


# --------------------------------------------------------------- end to end


async def test_discovery_lists_the_tests_that_exist(project: Path) -> None:
    service = TestService(project)

    frameworks, cases = await service.discover()

    pytest_entry = next(item for item in frameworks if item.id == "pytest")
    assert pytest_entry.available is True
    assert pytest_entry.test_count == 3
    names = {case.name for case in cases}
    assert names == {"test_passes", "test_fails_inside_a_helper", "test_skipped"}
    # The id is the framework's own selector, which is what makes a re-run
    # select exactly this test.
    assert any(case.id.endswith("::test_passes") for case in cases)


async def test_a_collection_error_is_reported_rather_than_zero_tests(
    tmp_path: Path,
) -> None:
    """ "0 tests" instead of the import error wastes everyone's afternoon."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'broken'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_broken.py").write_text("import a_module_that_is_not_there\n", encoding="utf-8")
    service = TestService(tmp_path)

    frameworks, cases = await service.discover()

    entry = next(item for item in frameworks if item.id == "pytest")
    assert cases == []
    assert entry.test_count == 0
    assert entry.detail  # the import error, not silence


async def test_a_run_reports_each_test_with_its_outcome(project: Path) -> None:
    service = TestService(project)

    run = await service.start()
    await service._task  # type: ignore[arg-type]
    finished = service.last
    assert finished is not None

    assert finished.id == run.id
    assert finished.status == "failed"
    tally = finished.counts
    assert tally["passed"] == 1
    assert tally["failed"] == 1
    # A skipped test is not a passing test; counting them together makes the
    # number stop meaning anything.
    assert tally["skipped"] == 1

    failure = next(item for item in finished.results if item.failed)
    assert failure.name == "test_fails_inside_a_helper"
    assert "helper says no" in failure.message
    # The failure happened in `helper`, not on the test's own line — and the
    # line someone wants to open is the one where it broke.
    assert failure.failure_file == "tests/test_things.py"
    assert failure.failure_line == 2


async def test_only_the_failures_are_re_run(project: Path) -> None:
    """Selection by the framework's ids, not by matching names."""
    service = TestService(project)
    await service.start()
    await service._task  # type: ignore[arg-type]

    selection = service.rerun_selection()
    assert len(selection) == 1
    assert selection[0].endswith("::test_fails_inside_a_helper")

    await service.start(selection=selection)
    await service._task  # type: ignore[arg-type]
    rerun = service.last
    assert rerun is not None

    assert len(rerun.results) == 1
    assert rerun.results[0].name == "test_fails_inside_a_helper"
    assert rerun.selection == selection


async def test_two_runs_at_once_are_refused(project: Path) -> None:
    """Tests share a working tree; two concurrent runs describe nothing."""
    service = TestService(project)
    await service.start()
    try:
        with pytest.raises(TestRunError, match="already in progress"):
            await service.start()
    finally:
        await service._task  # type: ignore[arg-type]


async def test_a_cancelled_run_says_so(project: Path) -> None:
    (project / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )
    service = TestService(project)
    await service.start()

    assert service.cancel() is True
    task = service._task
    assert task is not None
    with pytest.raises(BaseException):  # noqa: B017,PT011 - CancelledError
        await task
    assert service.last is not None
    assert service.last.status == "cancelled"


async def test_an_unknown_framework_is_refused(project: Path) -> None:
    service = TestService(project)
    with pytest.raises(TestRunError, match="Unknown test framework"):
        await service.start(framework_id="nosuchrunner")


# ---------------------------------------------------------------- parsers


def test_junit_reports_are_read_into_selectable_ids(tmp_path: Path) -> None:
    """pytest's dotted classname has to become a node id a re-run can use."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <testsuites><testsuite name="pytest" tests="2">
      <testcase classname="tests.unit.test_math.TestAdd" name="test_two"
                file="tests/unit/test_math.py" line="11" time="0.01"/>
      <testcase classname="tests.unit.test_math" name="test_bad"
                file="tests/unit/test_math.py" line="20" time="0.02">
        <failure message="assert 1 == 2">Traceback
  File "tests/unit/helpers.py", line 7, in check
    assert 1 == 2
AssertionError</failure>
      </testcase>
    </testsuite></testsuites>"""

    results = parse_junit(xml, tmp_path)

    assert results[0].id == "tests/unit/test_math.py::TestAdd::test_two"
    # JUnit's line is zero-based from pytest; the editor's is one-based.
    assert results[0].line == 12
    assert results[1].id == "tests/unit/test_math.py::test_bad"
    assert results[1].status == "failed"
    # The innermost frame is the cause, and the place to open.
    assert results[1].failure_file == "tests/unit/helpers.py"
    assert results[1].failure_line == 7


def test_a_setup_error_is_not_a_test_failure(tmp_path: Path) -> None:
    """A broken fixture must not read as a broken feature."""
    xml = """<testsuite name="pytest">
      <testcase classname="tests.test_a" name="test_x" file="tests/test_a.py" line="3">
        <error message="fixture 'db' not found">setup failed</error>
      </testcase>
    </testsuite>"""

    results = parse_junit(xml, tmp_path)

    assert results[0].status == "errored"
    assert results[0].failed is True


def test_skips_and_expected_failures_are_told_apart(tmp_path: Path) -> None:
    xml = """<testsuite name="pytest">
      <testcase classname="t" name="a"><skipped type="pytest.skip" message="nope"/></testcase>
      <testcase classname="t" name="b"><skipped type="pytest.xfail" message="known"/></testcase>
    </testsuite>"""

    results = parse_junit(xml, tmp_path)

    assert [item.status for item in results] == ["skipped", "xfailed"]


def test_jest_json_carries_the_failing_location(tmp_path: Path) -> None:
    """Preferred over Jest's JUnit output, which does not."""
    payload = """{"testResults": [{
      "name": "/repo/src/add.test.ts",
      "assertionResults": [
        {"title": "adds", "fullName": "math adds", "status": "passed",
         "ancestorTitles": ["math"], "duration": 12, "location": {"line": 4}},
        {"title": "breaks", "fullName": "math breaks", "status": "failed",
         "ancestorTitles": ["math"], "duration": 3, "location": {"line": 9},
         "failureMessages": ["Error: boom\\n    at src/add.ts:22:5"]}
      ]}]}"""

    results = parse_jest(payload, Path("/repo"))

    assert results[0].status == "passed"
    assert results[0].suite == "math"
    assert results[1].status == "failed"
    assert results[1].failure_file == "src/add.ts"
    assert results[1].failure_line == 22
    assert results[1].duration_seconds == pytest.approx(0.003)


def test_go_json_accumulates_output_before_deciding(tmp_path: Path) -> None:
    """A test's outcome is one event and its output is many."""
    stream = "\n".join(
        [
            '{"Action":"run","Package":"app/math","Test":"TestAdd"}',
            '{"Action":"output","Package":"app/math","Test":"TestAdd","Output":"ok\\n"}',
            '{"Action":"pass","Package":"app/math","Test":"TestAdd","Elapsed":0.02}',
            '{"Action":"output","Package":"app/math","Test":"TestSub",'
            '"Output":"    sub_test.go:14: wrong\\n"}',
            '{"Action":"fail","Package":"app/math","Test":"TestSub","Elapsed":0.01}',
            '{"Action":"output","Package":"app/math","Output":"FAIL\\n"}',
        ]
    )

    results = parse_go(stream, tmp_path)

    by_name = {item.name: item for item in results}
    assert by_name["TestAdd"].status == "passed"
    assert by_name["TestSub"].status == "failed"
    assert by_name["TestSub"].failure_file == "sub_test.go"
    assert by_name["TestSub"].failure_line == 14
    # The package-level output line is not a test.
    assert len(results) == 2


def test_coverage_is_read_not_inferred(tmp_path: Path) -> None:
    report = tmp_path / "cov.json"
    report.write_text(
        """{"files": {"app/core.py": {
             "summary": {"covered_lines": 8, "num_statements": 10},
             "missing_lines": [4, 9]}}}""",
        encoding="utf-8",
    )

    coverage = parse_coverage(report, "json", tmp_path)

    assert coverage is not None
    assert coverage.source == "coverage.py"
    assert (coverage.covered, coverage.total) == (8, 10)
    assert coverage.percent == pytest.approx(80.0)
    assert coverage.files[0].missing == [4, 9]


def test_a_missing_coverage_report_is_none_not_zero(tmp_path: Path) -> None:
    """A coverage number that was not measured is worse than none: it is believed."""
    assert parse_coverage(tmp_path / "absent.json", "json", tmp_path) is None


def test_istanbul_summaries_are_read(tmp_path: Path) -> None:
    report = tmp_path / "summary.json"
    report.write_text(
        """{"total": {"lines": {"covered": 30, "total": 40}},
            "/repo/src/a.ts": {"lines": {"covered": 30, "total": 40}}}""",
        encoding="utf-8",
    )

    coverage = parse_coverage(report, "json-summary", Path("/repo"))

    assert coverage is not None
    assert coverage.source == "istanbul"
    assert coverage.percent == pytest.approx(75.0)
    assert coverage.files[0].path == "src/a.ts"


async def test_coverage_is_collected_when_asked_for(project: Path) -> None:
    """Real numbers from the runner's own report, or none at all."""
    (project / "app.py").write_text(
        "def used():\n    return 1\n\n\ndef unused():\n    return 2\n", encoding="utf-8"
    )
    (project / "tests" / "test_things.py").write_text(
        "from app import used\n\n\ndef test_used():\n    assert used() == 1\n",
        encoding="utf-8",
    )
    service = TestService(project)

    await service.start(coverage=True)
    await service._task  # type: ignore[arg-type]
    run = service.last
    assert run is not None

    if run.coverage is None:
        pytest.skip("pytest-cov is not installed in this environment")
    assert run.coverage.source == "coverage.py"
    assert run.coverage.total > 0
    app = next(item for item in run.coverage.files if item.path.endswith("app.py"))
    # `unused` is never called, so its body is a missing line.
    assert app.missing
