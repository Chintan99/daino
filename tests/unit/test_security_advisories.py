"""Scanner output becomes findings the release gate can compare."""

from __future__ import annotations

import json

from daino.schemas import QACheck
from daino.security.advisories import findings_from_check, severity_of


def _check(check_id: str, payload: object, category: str = "security") -> QACheck:
    output = payload if isinstance(payload, str) else json.dumps(payload)
    return QACheck(id=check_id, label=check_id, category=category, output=output)  # type: ignore[arg-type]


def test_every_scanner_vocabulary_maps_onto_one_severity_scale() -> None:
    assert severity_of("CRITICAL") == "critical"
    assert severity_of("ERROR") == "high"
    assert severity_of("moderate") == "medium"
    assert severity_of("note") == "info"
    assert severity_of(9.8) == "critical"
    assert severity_of(5.1) == "medium"
    # An unrecognised word must not silently become the lowest severity.
    assert severity_of("weird") == "medium"


def test_bandit_results_keep_their_cwe_location_and_confidence() -> None:
    finding = findings_from_check(
        _check(
            "python-sast",
            {
                "results": [
                    {
                        "filename": "./app/run.py",
                        "line_number": 12,
                        "issue_severity": "HIGH",
                        "issue_confidence": "LOW",
                        "issue_text": "subprocess call with shell=True",
                        "issue_cwe": {"id": 78},
                        "test_id": "B602",
                        "code": "run(cmd, shell=True)",
                    }
                ]
            },
        )
    )[0]

    assert (finding.severity, finding.confidence) == ("high", "low")
    assert (finding.location, finding.line) == ("app/run.py", 12)
    assert (finding.cwe, finding.reference) == ("CWE-78", "B602")


def test_npm_advisories_say_whether_the_fix_is_breaking() -> None:
    findings = findings_from_check(
        _check(
            "js-audit",
            {
                "vulnerabilities": {
                    "lodash": {
                        "severity": "high",
                        "range": "<4.17.21",
                        "via": [{"title": "Prototype Pollution", "cwe": ["CWE-1321"]}],
                        "fixAvailable": {
                            "name": "lodash",
                            "version": "5.0.0",
                            "isSemVerMajor": True,
                        },
                    }
                }
            },
            category="dependencies",
        )
    )

    assert findings[0].cwe == "CWE-1321"
    assert "major version bump" in findings[0].remediation


def test_pip_audit_names_the_fixed_version() -> None:
    finding = findings_from_check(
        _check(
            "python-audit",
            {
                "dependencies": [
                    {"name": "safe", "version": "1.0", "vulns": []},
                    {
                        "name": "affected",
                        "version": "1.0",
                        "vulns": [
                            {
                                "id": "PYSEC-2024-1",
                                "fix_versions": ["1.1"],
                                "description": "SQL injection in the query builder",
                            }
                        ],
                    },
                ]
            },
            category="dependencies",
        )
    )[0]

    assert finding.severity == "high"  # inferred from "injection"
    assert "Upgrade affected to 1.1." == finding.remediation


def test_semgrep_gitleaks_osv_and_trivy_all_land_in_the_same_shape() -> None:
    semgrep = findings_from_check(
        _check(
            "semgrep",
            {
                "results": [
                    {
                        "check_id": "python.lang.security.audit.eval-detected",
                        "path": "svc.py",
                        "start": {"line": 4},
                        "extra": {
                            "message": "eval on user input",
                            "severity": "ERROR",
                            "metadata": {"cwe": ["CWE-95: Eval Injection"]},
                        },
                    }
                ]
            },
        )
    )[0]
    gitleaks = findings_from_check(
        _check(
            "secret-scan",
            [
                {
                    "RuleID": "aws-access-key",
                    "File": "cfg.py",
                    "StartLine": 2,
                    "Description": "AWS key",
                }
            ],
        )
    )[0]
    osv = findings_from_check(
        _check(
            "osv-scan",
            {
                "results": [
                    {
                        "packages": [
                            {
                                "package": {"name": "requests", "version": "2.0"},
                                "vulnerabilities": [
                                    {
                                        "id": "GHSA-x",
                                        "summary": "Header smuggling",
                                        "database_specific": {"severity": "MODERATE"},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
            category="dependencies",
        )
    )[0]
    trivy = findings_from_check(
        _check(
            "trivy-scan",
            {
                "Results": [
                    {
                        "Target": "Dockerfile",
                        "Misconfigurations": [
                            {"ID": "DS002", "Title": "Image user is root", "Severity": "HIGH"}
                        ],
                    }
                ]
            },
        )
    )[0]

    assert (semgrep.cwe, semgrep.severity, semgrep.line) == ("CWE-95", "high", 4)
    assert (gitleaks.category, gitleaks.severity) == ("secrets", "critical")
    # The secret's value is never carried into the report.
    assert "aws-access-key" in gitleaks.detail
    assert (osv.severity, osv.location) == ("medium", "requests")
    assert (trivy.category, trivy.severity) == ("configuration", "high")


def test_unparseable_or_unknown_output_yields_nothing_rather_than_a_guess() -> None:
    assert findings_from_check(_check("python-sast", "bandit: command not found")) == []
    assert findings_from_check(_check("python-sast", {"results": "unexpected"})) == []
    assert findings_from_check(_check("go-audit", {"anything": 1})) == []
    assert findings_from_check(_check("python-sast", "")) == []


def test_progress_chatter_before_the_json_does_not_defeat_a_parser() -> None:
    """CLIs print status lines to the same stream; the payload still parses."""
    noisy = 'scanning...\n{"results": [{"filename": "a.py", "test_id": "B101"}]}\ndone\n'

    assert len(findings_from_check(_check("python-sast", noisy))) == 1
