"""Reading what a test runner produced, in whatever shape it produced it.

Four readers, one output type. The JUnit one carries most of the weight because
most runners can emit it; the rest exist because their frameworks either cannot
(``go test``) or have something better of their own (Jest's JSON knows the
failing line, its JUnit output does not).

Two details are worth stating, because getting either wrong makes a Tests panel
quietly useless:

* **A failure's location is not the test's location.** A test defined at
  ``test_orders.py:12`` can fail inside a helper five files away, and the line
  someone wants to open is the second one. Both are kept, and they are separate
  fields.
* **A skipped test is not a passing test.** Runners report them together often
  enough that panels end up counting them together, and "142 passed" that
  includes 90 skips is a number that has stopped meaning anything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from daino.testing.models import Coverage, FileCoverage, TestCase, TestResult, TestStatus

#: A file:line pair in a traceback or assertion message. Used to find where a
#: failure happened when the report does not say outright.
_LOCATION = re.compile(
    r'(?:File "|at )?([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs))"?[,:]\s*(?:line )?(\d+)'
)


def _relative(path: str, root: Path) -> str:
    """Repository-relative, so the editor can open it."""
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def _locate(message: str, root: Path) -> tuple[str, int]:
    """The last file:line in a traceback — the innermost frame, i.e. the cause.

    Last rather than first on purpose: a Python traceback starts at the test and
    ends where it broke, and the end is what someone wants to open.
    """
    matches = _LOCATION.findall(message or "")
    if not matches:
        return "", 0
    path, line = matches[-1]
    return _relative(path, root), int(line)


# ------------------------------------------------------------------- JUnit


def parse_junit(xml: str, root: Path) -> list[TestResult]:
    """Read a JUnit XML report.

    Handles both shapes runners emit: a ``<testsuites>`` wrapper, or a bare
    ``<testsuite>``. Attribute names vary a little between producers, so the
    lookups are forgiving where it is safe to be.
    """
    try:
        tree = ElementTree.fromstring(xml)  # noqa: S314 - local runner output
    except ElementTree.ParseError:
        return []
    suites = (
        tree.iter("testsuite") if tree.tag in {"testsuites", "testsuite"} else iter(())
    )
    results: list[TestResult] = []
    for suite in suites:
        suite_name = suite.get("name", "")
        for case in suite.findall("testcase"):
            results.append(_junit_case(case, suite_name, root))
    return results


def _junit_case(case: Any, suite_name: str, root: Path) -> TestResult:
    name = case.get("name", "")
    classname = case.get("classname", "") or suite_name
    file = _relative(case.get("file", ""), root)
    line = int(case.get("line") or 0)
    # pytest reports a zero-based line in JUnit; every editor is one-based.
    if line and case.get("file"):
        line += 1

    status: TestStatus = "passed"
    message = ""
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    if failure is not None:
        status = "failed"
        message = _text(failure)
    elif error is not None:
        # Setup blew up: this test did not run, let alone fail. Keeping them
        # apart is what stops a broken fixture reading as a broken feature.
        status = "errored"
        message = _text(error)
    elif skipped is not None:
        status = "xfailed" if "xfail" in (skipped.get("type") or "").lower() else "skipped"
        message = _text(skipped)

    failure_file, failure_line = _locate(message, root)
    return TestResult(
        id=_junit_id(classname, name, file),
        name=name,
        suite=classname,
        file=file or failure_file,
        line=line,
        status=status,
        duration_seconds=float(case.get("time") or 0.0),
        message=message.strip(),
        failure_file=failure_file or file,
        failure_line=failure_line or line,
    )


def _junit_id(classname: str, name: str, file: str) -> str:
    """Rebuild the runner's own selector, so a re-run can name this test.

    pytest's classname is the module path with dots — "tests.unit.test_a" — plus
    any class. Turning that back into "tests/unit/test_a.py::test_b" is what
    makes "re-run failed" select exactly the right tests rather than a
    name-matching approximation.
    """
    if file and file.endswith(".py"):
        module = file.rsplit(".", 1)[0].replace("/", ".")
        trailing = classname[len(module) :].strip(".") if classname.startswith(module) else ""
        parts = [file, *(trailing.split(".") if trailing else []), name]
        return "::".join(part for part in parts if part)
    return f"{classname}::{name}" if classname else name


def _text(node: Any) -> str:
    """A JUnit failure's text: the attribute, the body, or both."""
    message = node.get("message") or ""
    body = (node.text or "").strip()
    if message and body and message.strip() not in body:
        return f"{message}\n{body}"
    return body or message


# -------------------------------------------------------------- Jest's JSON


def parse_jest(payload: str, root: Path) -> list[TestResult]:
    """Read ``jest --json``.

    Preferred over Jest's JUnit output because this carries the failing
    location, which the XML does not.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    results: list[TestResult] = []
    for suite in data.get("testResults") or []:
        file = _relative(str(suite.get("name", "")), root)
        for case in suite.get("assertionResults") or []:
            raw = str(case.get("status", "passed"))
            status: TestStatus = {
                "passed": "passed",
                "failed": "failed",
                "pending": "skipped",
                "skipped": "skipped",
                "todo": "skipped",
                "disabled": "skipped",
            }.get(raw, "failed")
            message = "\n".join(str(item) for item in case.get("failureMessages") or [])
            location = case.get("location") or {}
            line = int(location.get("line") or 0)
            failure_file, failure_line = _locate(message, root)
            title = str(case.get("fullName") or case.get("title") or "")
            results.append(
                TestResult(
                    id=f"{file} > {title}" if file else title,
                    name=str(case.get("title") or title),
                    suite=" > ".join(str(item) for item in case.get("ancestorTitles") or []),
                    file=file,
                    line=line,
                    status=status,
                    duration_seconds=float(case.get("duration") or 0) / 1000.0,
                    message=message.strip(),
                    failure_file=failure_file or file,
                    failure_line=failure_line or line,
                )
            )
    return results


# ------------------------------------------------------------ go test -json


def parse_go(stream: str, root: Path) -> list[TestResult]:
    """Read ``go test -json``.

    A stream of one JSON object per line, where a test's outcome is one event
    and its output is many, so both are accumulated per test before anything is
    decided.
    """
    outputs: dict[tuple[str, str], list[str]] = {}
    finished: dict[tuple[str, str], tuple[TestStatus, float]] = {}
    for line in stream.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        name = str(event.get("Test") or "")
        if not name:
            continue  # a package-level event, not a test
        key = (str(event.get("Package") or ""), name)
        action = str(event.get("Action") or "")
        if action == "output":
            outputs.setdefault(key, []).append(str(event.get("Output") or ""))
        elif action in {"pass", "fail", "skip"}:
            status: TestStatus = {
                "pass": "passed",
                "fail": "failed",
                "skip": "skipped",
            }[action]
            finished[key] = (status, float(event.get("Elapsed") or 0.0))

    results: list[TestResult] = []
    for (package, name), (status, elapsed) in finished.items():
        message = "".join(outputs.get((package, name), [])) if status != "passed" else ""
        failure_file, failure_line = _locate(message, root)
        results.append(
            TestResult(
                id=f"{package}::{name}" if package else name,
                name=name,
                suite=package,
                file=failure_file,
                line=failure_line,
                status=status,
                duration_seconds=elapsed,
                message=message.strip(),
                failure_file=failure_file,
                failure_line=failure_line,
            )
        )
    return results


# ------------------------------------------------------------------ coverage


def parse_coverage(path: Path, kind: str, root: Path) -> Coverage | None:
    """Read a coverage report, or return None when there is nothing to read.

    Only ever reads what the tool wrote. A coverage number that was inferred
    rather than measured is worse than no number, because it is believed.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if kind == "json":
        return _coverage_py(data, root)
    if kind == "json-summary":
        return _coverage_istanbul(data, root)
    return None


def _coverage_py(data: dict, root: Path) -> Coverage | None:
    """coverage.py's ``--cov-report json``."""
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    entries: list[FileCoverage] = []
    covered = total = 0
    for path, item in files.items():
        summary = item.get("summary") or {}
        hit = int(summary.get("covered_lines") or 0)
        statements = int(summary.get("num_statements") or 0)
        covered += hit
        total += statements
        entries.append(
            FileCoverage(
                path=_relative(str(path), root),
                covered=hit,
                total=statements,
                missing=[int(line) for line in (item.get("missing_lines") or [])][:500],
            )
        )
    entries.sort(key=lambda item: item.path)
    return Coverage(source="coverage.py", covered=covered, total=total, files=entries)


def _coverage_istanbul(data: dict, root: Path) -> Coverage | None:
    """Istanbul's ``json-summary``, which vitest and jest both produce."""
    totals = data.get("total")
    if not isinstance(totals, dict):
        return None
    lines = totals.get("lines") or {}
    entries = [
        FileCoverage(
            path=_relative(str(path), root),
            covered=int((item.get("lines") or {}).get("covered") or 0),
            total=int((item.get("lines") or {}).get("total") or 0),
        )
        for path, item in data.items()
        if path != "total" and isinstance(item, dict)
    ]
    entries.sort(key=lambda item: item.path)
    return Coverage(
        source="istanbul",
        covered=int(lines.get("covered") or 0),
        total=int(lines.get("total") or 0),
        files=entries,
    )


# ----------------------------------------------------------------- discovery


def parse_discovery(output: str, fmt: str, root: Path) -> list[TestCase]:
    """Turn a runner's "list the tests" output into cases."""
    if fmt == "pytest":
        return _discover_pytest(output)
    if fmt == "vitest-list":
        return _discover_vitest(output, root)
    if fmt == "paths":
        return _discover_paths(output, root)
    if fmt == "go-list":
        return _discover_go(output)
    return []


def _discover_pytest(output: str) -> list[TestCase]:
    """``pytest --collect-only -q`` prints one node id per line."""
    cases: list[TestCase] = []
    for line in output.splitlines():
        node = line.strip()
        if "::" not in node or node.startswith(("=", "-", "<", "E ")):
            continue
        file, _, remainder = node.partition("::")
        if not file.endswith(".py"):
            continue
        parts = remainder.split("::")
        cases.append(
            TestCase(
                id=node,
                name=parts[-1],
                suite="::".join(parts[:-1]),
                file=file,
            )
        )
    return cases


def _discover_vitest(output: str, root: Path) -> list[TestCase]:
    """``vitest list`` prints "path > suite > name" per line."""
    cases: list[TestCase] = []
    for line in output.splitlines():
        text = line.strip()
        if " > " not in text:
            continue
        file, _, remainder = text.partition(" > ")
        parts = [item.strip() for item in remainder.split(" > ")]
        cases.append(
            TestCase(
                id=text,
                name=parts[-1] if parts else text,
                suite=" > ".join(parts[:-1]),
                file=_relative(file.strip(), root),
            )
        )
    return cases


def _discover_paths(output: str, root: Path) -> list[TestCase]:
    """``jest --listTests`` prints one absolute file path per line."""
    cases: list[TestCase] = []
    for line in output.splitlines():
        text = line.strip()
        if not text or not text.startswith("/"):
            continue
        relative = _relative(text, root)
        cases.append(TestCase(id=relative, name=Path(relative).name, file=relative))
    return cases


def _discover_go(output: str) -> list[TestCase]:
    """``go test -list .`` prints test names, with package lines between them."""
    cases: list[TestCase] = []
    for line in output.splitlines():
        text = line.strip()
        if not text.startswith(("Test", "Benchmark", "Example", "Fuzz")):
            continue
        cases.append(TestCase(id=text, name=text))
    return cases
