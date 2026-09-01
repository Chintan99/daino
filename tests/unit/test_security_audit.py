"""The offline half of the Inspector: what it finds, and what it refuses to."""

from __future__ import annotations

from pathlib import Path

from daino.security.audit import (
    MAX_FINDINGS_PER_RULE,
    audit_repository,
    cap_per_rule,
    iter_text_files,
    scan_patterns,
    scan_secrets,
)


def _references(findings: list, location: str | None = None) -> set[str]:
    return {item.reference for item in findings if location is None or item.location == location}


def test_real_credentials_are_reported_and_placeholders_are_not() -> None:
    text = "\n".join(
        [
            "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'",
            "api_key = 'your-api-key-here'",
            "password = os.environ['DB_PASSWORD']",
            "token = 'xxxxxxxxxxxx'",
            "DATABASE_URL = 'postgresql://svc:Hunter2Pass@db.internal:5432/app'",
        ]
    )

    findings = scan_secrets("app/settings.py", text)
    references = {item.reference: item for item in findings}

    assert "secret-aws-access-key" in references
    assert references["secret-aws-access-key"].severity == "critical"
    assert "secret-connection-string" in references
    # A placeholder, an environment lookup, and a repeated-character stub are
    # the three ways a "credential" is usually not one.
    assert len(findings) == 2


def test_a_credential_never_leaves_the_report_in_clear_text() -> None:
    findings = scan_secrets("app.py", "GITHUB = 'ghp_" + "A" * 36 + "'")

    assert len(findings) == 1
    assert "ghp_" + "A" * 36 not in findings[0].detail
    assert "…" in findings[0].detail


def test_fixture_credentials_are_kept_but_cannot_block_a_release() -> None:
    line = "SECRET = 'AKIAIOSFODNN7EXAMPLE'"

    production = scan_secrets("app/config.py", line)[0]
    fixture = scan_secrets("tests/unit/test_config.py", line)[0]

    assert (production.severity, production.confidence) == ("critical", "high")
    assert (fixture.severity, fixture.confidence) == ("medium", "low")


def test_python_weaknesses_are_named_with_their_cwe() -> None:
    text = "\n".join(
        [
            "import subprocess, yaml, pickle",
            "subprocess.run(command, shell=True)",
            "config = yaml.load(raw)",
            "state = pickle.loads(payload)",
            "requests.get(url, verify=False)",
            "app.run(host='0.0.0.0', debug=True)",
        ]
    )

    findings = scan_patterns("service.py", text)
    by_rule = {item.reference: item for item in findings}

    assert by_rule["py-shell-injection"].cwe == "CWE-78"
    assert by_rule["py-yaml-load"].cwe == "CWE-502"
    assert by_rule["py-insecure-deserialization"].cwe == "CWE-502"
    assert by_rule["py-tls-verification-disabled"].cwe == "CWE-295"
    assert by_rule["py-debug-server"].severity == "high"


def test_a_handled_weakness_is_not_reported() -> None:
    """The safe form of a risky call must not produce the risky call's finding."""
    assert "py-yaml-load" not in _references(
        scan_patterns("service.py", "config = yaml.load(raw, Loader=yaml.SafeLoader)")
    )
    assert "py-weak-hash" not in _references(
        scan_patterns("service.py", "digest = hashlib.md5(data, usedforsecurity=False)")
    )


def test_commented_and_pattern_defining_lines_are_not_findings() -> None:
    """A repository containing security tooling must not report its own rules.

    Without this, every scanner, linter, and piece of documentation that names a
    weakness gets reported as having it.
    """
    text = "\n".join(
        [
            "# subprocess.run(cmd, shell=True)",
            'RULE = re.compile(r"shell\\s*=\\s*True")',
            '"Never pass shell=True to subprocess."',
        ]
    )

    assert scan_patterns("scanner.py", text) == []


def test_environment_files_must_be_ignored_by_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=live\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")

    findings = audit_repository(tmp_path)
    hygiene = _references(findings)

    assert "env-not-ignored" in hygiene
    # The example file is the documented pattern, not a leak.
    assert [item for item in findings if item.reference == "env-not-ignored"][0].location == ".env"

    (tmp_path / ".gitignore").write_text("node_modules/\n.env*\n", encoding="utf-8")
    assert "env-not-ignored" not in _references(audit_repository(tmp_path))


def test_a_root_container_is_reported_once_the_image_is_defined(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nRUN pip install .\nCMD ['app']\n", encoding="utf-8"
    )

    assert "docker-runs-as-root" in _references(audit_repository(tmp_path))

    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nRUN pip install .\nUSER app\nCMD ['app']\n",
        encoding="utf-8",
    )
    assert "docker-runs-as-root" not in _references(audit_repository(tmp_path))


def test_dependency_directories_are_never_read(tmp_path: Path) -> None:
    """Pruning vendored trees is what keeps the audit usable on a real checkout."""
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("eval(x)", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("eval(x)", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    read = {path for path, _ in iter_text_files(tmp_path)}

    assert read == {"src/index.js"}


def test_one_repeated_rule_cannot_bury_the_rest_of_the_report() -> None:
    from daino.schemas import QAFinding

    noisy = [
        QAFinding(id=f"n{index}", title="Repeated", reference="noisy", location=f"f{index}.py")
        for index in range(MAX_FINDINGS_PER_RULE + 7)
    ]
    other = QAFinding(id="other", title="Distinct", reference="quiet", location="a.py")

    capped = cap_per_rule([*noisy, other])

    assert sum(item.reference == "noisy" for item in capped) == MAX_FINDINGS_PER_RULE + 1
    assert other in capped
    overflow = next(item for item in capped if item.id == "noisy:overflow")
    assert overflow.severity == "info" and "7 more" in overflow.detail
