"""Detecting test frameworks, and knowing how to drive each one.

One idea holds this together: **ask every runner for JUnit XML**. Nearly all of
them can emit it, it carries exactly what a Tests panel needs — per-test status,
duration, file, line, and the failure text — and it means the parser is written
once instead of once per framework. Scraping a runner's human-readable output is
how a test panel comes to disagree with the terminal, which is the one thing it
must never do.

Where a runner cannot emit JUnit XML, it gets a purpose-built reader (`go test
-json`), and where neither is possible the run still reports its exit status and
output rather than pretending to know more than it does.

Nothing here executes anything. It answers "what is here, and what would you
run?"; :mod:`daino.testing.runner` does the running.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Where a runner is told to write its JUnit report. Under the state directory
#: rather than the project, so a test run never dirties the working tree — which
#: would otherwise make every run look like an uncommitted change.
REPORT_DIRNAME = "test-reports"


@dataclass(frozen=True, slots=True)
class Framework:
    """How to discover, run, and read one test framework."""

    id: str
    label: str
    #: Base argv, before selection or report flags.
    argv: tuple[str, ...]
    #: How results come back: a JUnit XML file, or a stream this runner's own
    #: reader understands.
    report: str = "junit"
    #: Flag template for the JUnit output path, e.g. "--junit-xml={path}".
    report_flag: str = ""
    #: Argv that lists tests without running them.
    discover_argv: tuple[str, ...] = ()
    #: How discovery output is read.
    discover_format: str = ""
    #: Flags that turn coverage on, and where its report lands.
    coverage_argv: tuple[str, ...] = ()
    coverage_report: str = ""
    #: Extra flags for a re-run of specific tests. Empty means "append ids".
    select_prefix: str = ""
    #: Human-facing note when something optional is missing.
    detail: str = ""


def _python(root: Path) -> str:
    """The interpreter for this project.

    Never the bare name ``python``: it does not exist on modern macOS or most
    Linux distributions, and a test runner that cannot be started is worse than
    one that is absent — it reports failures that are not there.
    """
    for relative in (Path(".venv/bin/python"), Path(".venv/Scripts/python.exe")):
        if (root / relative).is_file():
            return str(root / relative)
    return sys.executable or "python3"


def _node_bin(root: Path, name: str) -> str | None:
    local = root / "node_modules" / ".bin" / name
    if local.is_file():
        return str(local)
    return shutil.which(name)


def _package_json(root: Path) -> dict:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _has_module(root: Path, module: str) -> bool:
    """Whether the project's interpreter can import ``module``."""
    import subprocess  # noqa: PLC0415 - a capability probe, never a shell

    try:
        return (
            subprocess.run(  # noqa: S603
                [_python(root), "-c", f"import {module}"],
                cwd=str(root),
                capture_output=True,
                check=False,
                timeout=20,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def detect(root: Path) -> list[Framework]:
    """Every test framework this project appears to use.

    Ordered by how likely it is to be the one someone means: a repository with
    both a Python backend and a JS frontend gets both, backend first, and the
    panel lets the user choose.
    """
    found: list[Framework] = []
    python_project = any(
        (root / name).exists()
        for name in ("pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini")
    )
    if python_project or (root / "tests").is_dir():
        interpreter = _python(root)
        has_pytest = _has_module(root, "pytest")
        has_cov = has_pytest and _has_module(root, "pytest_cov")
        found.append(
            Framework(
                id="pytest",
                label="pytest",
                argv=(interpreter, "-m", "pytest"),
                report="junit",
                # xunit1 rather than the default xunit2, which omits the `file`
                # and `line` attributes entirely. Without them a result cannot
                # be turned back into a pytest node id, so "re-run the failures"
                # degrades into name matching and "go to the test" cannot work.
                report_flag="--junit-xml={path} -o junit_family=xunit1",
                discover_argv=(interpreter, "-m", "pytest", "--collect-only", "-q"),
                discover_format="pytest",
                coverage_argv=(
                    ("--cov", "--cov-report", "json:{coverage}") if has_cov else ()
                ),
                coverage_report="json",
                detail=(
                    ""
                    if has_pytest
                    else "pytest is not installed in this project's interpreter."
                ),
            )
        )

    package = _package_json(root)
    scripts = package.get("scripts") or {}
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    if "vitest" in dependencies or any(root.glob("vitest.config.*")):
        binary = _node_bin(root, "vitest")
        found.append(
            Framework(
                id="vitest",
                label="Vitest",
                argv=((binary, "run") if binary else ()),
                report="junit",
                # Vitest writes the reporter's output where --outputFile says.
                report_flag="--reporter=junit --outputFile={path}",
                discover_argv=((binary, "list") if binary else ()),
                discover_format="vitest-list",
                coverage_argv=("--coverage", "--coverage.reporter=json-summary"),
                coverage_report="json-summary",
                select_prefix="-t",
                detail="" if binary else "vitest is not installed in node_modules.",
            )
        )
    elif "jest" in dependencies or any(root.glob("jest.config.*")):
        binary = _node_bin(root, "jest")
        found.append(
            Framework(
                id="jest",
                label="Jest",
                argv=((binary,) if binary else ()),
                # Jest's own JSON is richer than what jest-junit would give, and
                # needs no extra package installed.
                report="jest-json",
                report_flag="--json --outputFile={path}",
                discover_argv=((binary, "--listTests") if binary else ()),
                discover_format="paths",
                coverage_argv=("--coverage", "--coverageReporters=json-summary"),
                coverage_report="json-summary",
                select_prefix="-t",
                detail="" if binary else "jest is not installed in node_modules.",
            )
        )
    elif "test" in scripts and package:
        # A project with a test script but no framework we recognise still gets
        # a run button; it just cannot report per-test results.
        manager = "yarn" if (root / "yarn.lock").is_file() else "npm"
        found.append(
            Framework(
                id="npm-test",
                label=f"{manager} test",
                argv=((manager, "test") if shutil.which(manager) else ()),
                report="none",
                detail=(
                    "This project's test script is run as-is; per-test results "
                    "need vitest or jest."
                ),
            )
        )

    if (root / "go.mod").is_file():
        binary = shutil.which("go")
        found.append(
            Framework(
                id="go",
                label="go test",
                argv=((binary, "test", "-json", "./...") if binary else ()),
                report="go-json",
                discover_argv=((binary, "test", "-list", ".", "./...") if binary else ()),
                discover_format="go-list",
                coverage_argv=("-cover",),
                select_prefix="-run",
                detail="" if binary else "go is not installed.",
            )
        )

    if (root / "Cargo.toml").is_file():
        binary = shutil.which("cargo")
        found.append(
            Framework(
                id="cargo",
                label="cargo test",
                argv=((binary, "test") if binary else ()),
                report="none",
                detail=(
                    ""
                    if binary
                    else "cargo is not installed."
                ),
            )
        )
    return found


def by_id(root: Path, framework_id: str) -> Framework | None:
    return next((item for item in detect(root) if item.id == framework_id), None)


@dataclass(slots=True)
class Invocation:
    """A fully built command, and where to read its results from."""

    argv: list[str]
    report_path: Path | None = None
    coverage_path: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """The command as a person would type it, for the run's record."""
        return shlex.join(self.argv)


def build(
    framework: Framework,
    reports: Path,
    *,
    selection: list[str] | None = None,
    coverage: bool = False,
) -> Invocation:
    """Assemble the argv for one run.

    ``selection`` re-runs specific tests. Each framework selects differently —
    pytest takes node ids as positional arguments, vitest and jest take a name
    pattern, `go test` takes a `-run` regex — so this is the one place that
    knows the difference, and callers pass the ids they were given back.
    """
    if not framework.argv:
        raise ValueError(framework.detail or f"{framework.label} is not available.")
    argv = list(framework.argv)
    report_path: Path | None = None
    coverage_path: Path | None = None

    if framework.report_flag:
        reports.mkdir(parents=True, exist_ok=True)
        suffix = "json" if framework.report in {"jest-json"} else "xml"
        report_path = reports / f"{framework.id}.{suffix}"
        coverage_path = reports / f"{framework.id}-coverage.json"
        argv.extend(
            shlex.split(
                framework.report_flag.format(
                    path=str(report_path), coverage=str(coverage_path)
                )
            )
        )

    if coverage and framework.coverage_argv:
        reports.mkdir(parents=True, exist_ok=True)
        coverage_path = coverage_path or (reports / f"{framework.id}-coverage.json")
        argv.extend(
            item.format(coverage=str(coverage_path)) for item in framework.coverage_argv
        )
    elif not coverage:
        coverage_path = None

    if selection:
        if framework.select_prefix:
            # A name-pattern runner takes one alternation rather than N flags.
            argv.extend([framework.select_prefix, "|".join(selection)])
        else:
            argv.extend(selection)

    return Invocation(argv=argv, report_path=report_path, coverage_path=coverage_path)


def discovery_command(framework: Framework) -> list[str]:
    return list(framework.discover_argv)
