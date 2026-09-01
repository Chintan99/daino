from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from daino.agents.team import validate_team_plan
from daino.agents.tool_schemas import QA_TOOL_SPECS
from daino.application.qa_service import (
    HIGH_FINDING_BLOCK_THRESHOLD,
    _dependency_summary,
    discover_checks,
    evaluate_gate,
    inspect_project,
    merge_duplicates,
    specialist_plan,
)
from daino.schemas import QAAgentAction, QACheck, QAFinding, QAReport, QASeverity


def _tool_names() -> set[str]:
    return {str(spec["function"]["name"]) for spec in QA_TOOL_SPECS}


def test_qa_tool_surface_is_read_only() -> None:
    assert _tool_names() == {
        "read_file",
        "search_text",
        "glob",
        "grep",
        "list_directory",
        "finish",
    }
    with pytest.raises(ValidationError):
        QAAgentAction(
            thought="change the target",
            action="write",
            path="app.py",
            content="unsafe",
        )


def test_project_detection_and_checks_cover_full_stack_playwright_and_audits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "lint": "eslint .",
                    "test": "vitest run",
                    "build": "vite build",
                    "test:e2e": "playwright test",
                },
                "dependencies": {"react": "19.0.0", "express": "5.0.0"},
                "devDependencies": {"@playwright/test": "1.52.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='sample'\n[tool.ruff]\nline-length=100\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(
        "daino.application.qa_service.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    profile = inspect_project(tmp_path)
    checks = {item.id: item for item in discover_checks(tmp_path, profile)}

    assert profile.frontend and profile.backend and profile.playwright
    assert {"frontend", "backend", "npm", "python", "playwright"} <= set(profile.labels)
    assert checks["playwright"].command == "npm run test:e2e"
    assert checks["js-audit"].command == "npm audit --json"
    assert checks["js-audit"].network_required
    assert checks["python-audit"].command.endswith("pip-audit --format json .")
    assert {"python-lint", "python-tests", "js-lint", "js-test", "js-build"} <= checks.keys()


def test_playwright_is_transparently_skipped_when_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# no browser app\n", encoding="utf-8")

    profile = inspect_project(tmp_path)
    check = next(item for item in discover_checks(tmp_path, profile) if item.id == "playwright")

    assert not profile.playwright
    assert check.status == "skipped"
    assert "Not applicable" in check.summary


def test_specialist_roster_fans_out_then_synthesizes() -> None:
    profile = inspect_project(Path(__file__).parents[2])
    profile = profile.__class__(
        labels=("frontend", "backend", "playwright"),
        frontend=True,
        backend=True,
        playwright=True,
        package_manager="npm",
    )

    plan = specialist_plan(profile)
    waves = validate_team_plan(plan)

    assert [[item.id for item in wave] for wave in waves] == [
        [
            "architecture",
            "backend",
            "code-quality",
            "frontend",
            "security",
            "supply-chain",
            "threat-model",
            "ui-browser",
        ],
        ["qa-summary"],
    ]
    assert all(member.read_only and member.scope == [] for member in plan.members)
    assert set(plan.members[-1].dependencies) == {
        "architecture",
        "backend",
        "code-quality",
        "frontend",
        "security",
        "supply-chain",
        "threat-model",
        "ui-browser",
    }


def test_scan_profile_selects_only_the_relevant_specialists() -> None:
    profile = inspect_project(Path(__file__).parents[2])
    profile = profile.__class__(
        labels=("frontend", "backend"),
        frontend=True,
        backend=True,
        playwright=False,
        package_manager="npm",
    )

    security_only = {member.id for member in specialist_plan(profile, "security").members}
    quality_only = {member.id for member in specialist_plan(profile, "quality").members}

    assert security_only == {"security", "threat-model", "supply-chain", "qa-summary"}
    assert quality_only == {
        "architecture",
        "code-quality",
        "frontend",
        "ui-browser",
        "backend",
        "qa-summary",
    }
    # The two halves are disjoint apart from the synthesis step that reads both.
    assert security_only & quality_only == {"qa-summary"}


def test_dependency_outputs_are_summarized_for_the_report() -> None:
    assert (
        _dependency_summary(
            json.dumps(
                {
                    "metadata": {
                        "vulnerabilities": {
                            "info": 0,
                            "low": 1,
                            "moderate": 2,
                            "high": 1,
                            "critical": 0,
                            "total": 4,
                        }
                    }
                }
            )
        )
        == "4 JavaScript vulnerability finding(s) reported."
    )
    assert (
        _dependency_summary(
            json.dumps(
                [
                    {"name": "safe", "vulns": []},
                    {"name": "affected", "vulns": [{"id": "PYSEC-1"}]},
                ]
            )
        )
        == "1 vulnerable Python package(s) reported."
    )


def _report(
    findings: list[QAFinding] | None = None,
    checks: list[QACheck] | None = None,
    status: str = "completed",
) -> QAReport:
    return QAReport(
        id="qa-1",
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        findings=findings or [],
        checks=checks or [],
    )


def _finding(severity: QASeverity, confidence: str = "high") -> QAFinding:
    return QAFinding(
        id=f"f-{severity}-{confidence}",
        title=f"a {severity} problem",
        severity=severity,
        confidence=confidence,  # type: ignore[arg-type]
    )


def test_security_scanners_are_discovered_only_when_installed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("daino.application.qa_service.shutil.which", lambda name: "")
    monkeypatch.setattr("daino.application.qa_service.Path.is_file", lambda self: False)

    checks = {item.id: item for item in discover_checks(tmp_path, inspect_project(tmp_path))}

    # The scan is still transparent about what it could not run.
    for identifier in ("python-sast", "secret-scan", "semgrep", "osv-scan", "trivy-scan"):
        assert checks[identifier].status == "skipped"
        assert "not installed" in checks[identifier].summary


def test_the_scan_profile_decides_which_commands_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n[tool.ruff]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(
        "daino.application.qa_service.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    profile = inspect_project(tmp_path)

    quality = {item.id for item in discover_checks(tmp_path, profile, "quality")}
    security = {item.id for item in discover_checks(tmp_path, profile, "security")}

    assert {"python-lint", "python-tests"} <= quality
    assert not ({"python-sast", "python-audit", "semgrep"} & quality)
    assert {"python-sast", "python-audit", "semgrep", "osv-scan"} <= security
    assert not ({"python-lint", "python-tests", "playwright"} & security)


def test_a_critical_finding_or_a_failing_test_blocks_the_release() -> None:
    critical, reasons = evaluate_gate(_report(findings=[_finding("critical")]))
    failing_tests, test_reasons = evaluate_gate(
        _report(checks=[QACheck(id="t", label="Python tests", category="tests", status="failed")])
    )

    assert critical == "blocked"
    assert "1 critical finding(s)" in reasons[0]
    assert failing_tests == "blocked"
    assert "Python tests" in test_reasons[0]


def test_high_findings_block_only_once_they_cluster() -> None:
    few = [_finding("high") for _ in range(HIGH_FINDING_BLOCK_THRESHOLD - 1)]
    many = [_finding("high") for _ in range(HIGH_FINDING_BLOCK_THRESHOLD)]

    assert evaluate_gate(_report(findings=few))[0] == "warn"
    assert evaluate_gate(_report(findings=many))[0] == "blocked"


def test_low_confidence_findings_are_reported_but_never_block() -> None:
    """A credential shape in a fixture must not be what stops a release."""
    report = _report(findings=[_finding("critical", confidence="low")])

    verdict, reasons = evaluate_gate(report)

    assert verdict == "pass"
    assert report.findings  # still in the report
    assert "No critical or high findings" in reasons[0]


def test_missing_security_evidence_is_a_warning_not_a_pass() -> None:
    """Nothing was found because nothing ran; that is not the same as clean."""
    report = _report(
        checks=[
            QACheck(
                id="semgrep",
                label="Semgrep static analysis",
                category="security",
                status="skipped",
                summary="semgrep is not installed; check was not run.",
            )
        ]
    )

    verdict, reasons = evaluate_gate(report)

    assert verdict == "warn"
    assert "security evidence is incomplete" in reasons[0]


def test_a_narrowed_scan_says_what_it_did_not_look_at() -> None:
    """A quality-only pass must not read as a security clearance."""
    quality = _report()
    quality.scan_profile = "quality"
    security = _report()
    security.scan_profile = "security"

    quality_verdict, quality_reasons = evaluate_gate(quality)
    _, security_reasons = evaluate_gate(security)
    _, full_reasons = evaluate_gate(_report())

    assert quality_verdict == "pass"
    assert "no vulnerability assessment was run" in quality_reasons[-1]
    assert "tests and quality checks were not run" in security_reasons[-1]
    assert not any("Scope:" in reason for reason in full_reasons)


def test_an_unfinished_inspection_clears_nothing() -> None:
    assert evaluate_gate(_report(status="cancelled"))[0] == "unknown"
    assert evaluate_gate(_report(status="failed"))[0] == "unknown"


def test_one_weakness_seen_by_two_scanners_is_reported_once() -> None:
    """The tally the gate reads must count problems, not reports of them."""
    shared = {"location": "app.py", "line": 4, "cwe": "CWE-78"}
    builtin = QAFinding(
        id="builtin",
        title="Subprocess invoked through a shell",
        severity="high",
        confidence="medium",
        source="built-in code audit",
        **shared,
    )
    scanner = QAFinding(
        id="bandit",
        title="subprocess call with shell=True",
        severity="high",
        confidence="high",
        source="bandit",
        **shared,
    )
    unrelated = QAFinding(
        id="other", title="Cookie missing HttpOnly", location="http://127.0.0.1:8000/"
    )

    merged = merge_duplicates([builtin, scanner, unrelated])

    assert len(merged) == 2
    # The more confident source wins the row, but both are credited.
    assert merged[0].id == "bandit"
    assert merged[0].source == "built-in code audit, bandit"
    assert unrelated in merged


def test_findings_without_a_source_location_are_never_merged() -> None:
    """Two advisories on two packages share nothing but a severity."""
    first = QAFinding(id="a", title="requests is vulnerable", location="requests")
    second = QAFinding(id="b", title="urllib3 is vulnerable", location="urllib3")

    assert merge_duplicates([first, second]) == [first, second]
