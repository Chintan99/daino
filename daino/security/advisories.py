"""Turn security-scanner output into the Inspector's own finding records.

Every scanner has its own JSON shape, its own severity vocabulary, and its own
idea of what a location is. The release gate has to compare a Bandit result
against an npm advisory against a Semgrep hit, so each supported tool gets a
small parser here and the rest of the system only ever sees :class:`QAFinding`.

A parser that does not recognise its input returns nothing rather than
guessing: an unparsed scan still shows its raw output as evidence in the
report, and a fabricated finding would be worse than a missing one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from daino.schemas import QACheck, QAFinding, QAFindingCategory, QASeverity

#: Every vocabulary the supported scanners use, mapped onto ours.
_SEVERITY_ALIASES: dict[str, QASeverity] = {
    "critical": "critical",
    "error": "high",
    "high": "high",
    "important": "high",
    "moderate": "medium",
    "medium": "medium",
    "warning": "medium",
    "low": "low",
    "minor": "low",
    "note": "info",
    "info": "info",
    "informational": "info",
    "unknown": "medium",
    "none": "info",
}

_CWE = re.compile(r"CWE-\d+")


def severity_of(value: object, default: QASeverity = "medium") -> QASeverity:
    """Normalise any scanner's severity word, falling back rather than failing."""
    if isinstance(value, (int, float)):
        return _from_cvss(float(value))
    if not isinstance(value, str):
        return default
    return _SEVERITY_ALIASES.get(value.strip().casefold(), default)


def findings_from_check(check: QACheck) -> list[QAFinding]:
    """Parse one finished check's output into findings.

    Dispatch is on the check id because that is what :mod:`daino.application.
    qa_service` controls; the command line behind it can change without
    silently disabling a parser.
    """
    if not check.output.strip():
        return []
    parser = _PARSERS.get(check.id)
    if parser is None:
        return []
    try:
        return parser(check)
    except (ValueError, TypeError, KeyError, AttributeError):
        # Scanner output is untrusted input like any other command output. A
        # shape we did not anticipate must degrade to "no findings parsed",
        # never to a failed inspection.
        return []


# ------------------------------------------------------------------ parsers


def _parse_bandit(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    results = payload.get("results") if payload else None
    if not isinstance(results, list):
        return []
    findings: list[QAFinding] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        cwe = item.get("issue_cwe")
        cwe_id = f"CWE-{cwe['id']}" if isinstance(cwe, dict) and cwe.get("id") else ""
        location = str(item.get("filename", "")).removeprefix("./")
        test_id = str(item.get("test_id", ""))
        line = item.get("line_number")
        findings.append(
            QAFinding(
                id=f"bandit-{test_id}:{location}:{line}",
                title=str(item.get("issue_text", "Bandit finding"))[:200],
                severity=severity_of(item.get("issue_severity")),
                category="vulnerability",
                source="bandit",
                location=location,
                line=line if isinstance(line, int) else None,
                detail=str(item.get("code", ""))[:400].strip(),
                remediation=str(item.get("more_info", "")),
                cwe=cwe_id,
                reference=test_id,
                confidence=_confidence(item.get("issue_confidence")),
            )
        )
    return findings


def _parse_pip_audit(check: QACheck) -> list[QAFinding]:
    payload = _json_value(check.output)
    if isinstance(payload, dict):
        dependencies = payload.get("dependencies")
    else:
        dependencies = payload
    if not isinstance(dependencies, list):
        return []
    findings: list[QAFinding] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        name = str(dependency.get("name", "?"))
        version = str(dependency.get("version", "?"))
        for vulnerability in dependency.get("vulns") or []:
            if not isinstance(vulnerability, dict):
                continue
            identifier = str(vulnerability.get("id", "?"))
            fixes = [str(item) for item in vulnerability.get("fix_versions") or []]
            findings.append(
                QAFinding(
                    id=f"pip-audit-{identifier}-{name}",
                    title=f"{name} {version} is affected by {identifier}",
                    severity=_advisory_severity(identifier, vulnerability.get("description")),
                    category="dependencies",
                    source="pip-audit",
                    location=name,
                    detail=str(vulnerability.get("description", ""))[:600],
                    remediation=(
                        f"Upgrade {name} to {', '.join(fixes)}."
                        if fixes
                        else f"No fixed version is published for {name}; assess whether the "
                        "affected code path is reachable."
                    ),
                    reference=identifier,
                    confidence="high",
                )
            )
    return findings


def _parse_npm_audit(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    if not payload:
        return []
    findings: list[QAFinding] = []
    vulnerabilities = payload.get("vulnerabilities")
    if isinstance(vulnerabilities, dict):
        for name, entry in vulnerabilities.items():
            if not isinstance(entry, dict):
                continue
            titles: list[str] = []
            urls: list[str] = []
            cwes: list[str] = []
            for via in entry.get("via") or []:
                if isinstance(via, dict):
                    titles.append(str(via.get("title", "")))
                    urls.append(str(via.get("url", "")))
                    cwes.extend(str(item) for item in via.get("cwe") or [])
            fix = entry.get("fixAvailable")
            findings.append(
                QAFinding(
                    id=f"npm-audit-{name}",
                    title=f"{name}: {titles[0] or 'known vulnerability'}"[:200],
                    severity=severity_of(entry.get("severity")),
                    category="dependencies",
                    source="npm audit",
                    location=str(name),
                    detail=(
                        f"Affected range {entry.get('range', '?')}. "
                        + " ".join(item for item in urls if item)
                    )[:600],
                    remediation=_npm_remediation(str(name), fix),
                    cwe=next((item for item in cwes if _CWE.fullmatch(item)), ""),
                    reference=str(name),
                    confidence="high",
                )
            )
        return findings
    advisories = payload.get("advisories")
    if isinstance(advisories, dict):
        for entry in advisories.values():
            if not isinstance(entry, dict):
                continue
            module = str(entry.get("module_name", "?"))
            findings.append(
                QAFinding(
                    id=f"npm-audit-{entry.get('id', module)}",
                    title=f"{module}: {entry.get('title', 'known vulnerability')}"[:200],
                    severity=severity_of(entry.get("severity")),
                    category="dependencies",
                    source="npm audit",
                    location=module,
                    detail=str(entry.get("overview", ""))[:600],
                    remediation=str(entry.get("recommendation", "")),
                    cwe=str(entry.get("cwe", "")) if _CWE.fullmatch(str(entry.get("cwe"))) else "",
                    reference=module,
                    confidence="high",
                )
            )
    return findings


def _parse_semgrep(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    results = payload.get("results") if payload else None
    if not isinstance(results, list):
        return []
    findings: list[QAFinding] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        extra = _sub(item, "extra")
        metadata = _sub(extra, "metadata")
        start = _sub(item, "start")
        rule = str(item.get("check_id", "semgrep"))
        line = start.get("line")
        cwe_values = metadata.get("cwe")
        cwe_text = " ".join(cwe_values) if isinstance(cwe_values, list) else str(cwe_values or "")
        match = _CWE.search(cwe_text)
        findings.append(
            QAFinding(
                id=f"semgrep-{rule}:{item.get('path')}:{line}",
                title=str(extra.get("message", rule))[:200],
                severity=severity_of(extra.get("severity")),
                category="vulnerability",
                source="semgrep",
                location=str(item.get("path", "")),
                line=line if isinstance(line, int) else None,
                detail=str(extra.get("lines", ""))[:400].strip(),
                remediation=str(extra.get("fix") or metadata.get("references") or ""),
                cwe=match.group(0) if match else "",
                reference=rule,
                confidence=_confidence(metadata.get("confidence")),
            )
        )
    return findings


def _parse_osv(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    results = payload.get("results") if payload else None
    if not isinstance(results, list):
        return []
    findings: list[QAFinding] = []
    for result in results:
        for package in (result or {}).get("packages") or []:
            if not isinstance(package, dict):
                continue
            info = _sub(package, "package")
            name = str(info.get("name", "?"))
            version = str(info.get("version", "?"))
            for vulnerability in package.get("vulnerabilities") or []:
                if not isinstance(vulnerability, dict):
                    continue
                identifier = str(vulnerability.get("id", "?"))
                specific = vulnerability.get("database_specific")
                declared = specific.get("severity") if isinstance(specific, dict) else None
                findings.append(
                    QAFinding(
                        id=f"osv-{identifier}-{name}",
                        title=f"{name} {version} is affected by {identifier}",
                        severity=severity_of(
                            declared, _advisory_severity(identifier, vulnerability.get("summary"))
                        ),
                        category="dependencies",
                        source="osv-scanner",
                        location=name,
                        detail=str(vulnerability.get("summary", ""))[:600],
                        remediation=f"Upgrade {name} past the affected range.",
                        reference=identifier,
                        confidence="high",
                    )
                )
    return findings


def _parse_gitleaks(check: QACheck) -> list[QAFinding]:
    payload = _json_value(check.output)
    if not isinstance(payload, list):
        return []
    findings: list[QAFinding] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("RuleID") or item.get("ruleID") or "gitleaks")
        location = str(item.get("File") or item.get("file") or "")
        line = item.get("StartLine") or item.get("startLine")
        findings.append(
            QAFinding(
                id=f"gitleaks-{rule}:{location}:{line}",
                title=str(item.get("Description") or "Secret detected by gitleaks")[:200],
                severity="critical",
                category="secrets",
                source="gitleaks",
                location=location,
                line=line if isinstance(line, int) else None,
                detail=f"Rule {rule} matched. The value itself is not reproduced here.",
                remediation=(
                    "Rotate the credential, purge it from git history, and load it from the "
                    "environment instead."
                ),
                cwe="CWE-798",
                reference=rule,
                confidence="high",
            )
        )
    return findings


def _parse_cargo_audit(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    section = payload.get("vulnerabilities") if payload else None
    entries = section.get("list") if isinstance(section, dict) else None
    if not isinstance(entries, list):
        return []
    findings: list[QAFinding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        advisory = _sub(entry, "advisory")
        package = _sub(entry, "package")
        identifier = str(advisory.get("id", "?"))
        name = str(package.get("name", "?"))
        findings.append(
            QAFinding(
                id=f"cargo-audit-{identifier}-{name}",
                title=f"{name}: {advisory.get('title', identifier)}"[:200],
                severity=_advisory_severity(identifier, advisory.get("description")),
                category="dependencies",
                source="cargo-audit",
                location=name,
                detail=str(advisory.get("description", ""))[:600],
                remediation=f"Upgrade {name} to a patched release.",
                reference=identifier,
                confidence="high",
            )
        )
    return findings


def _parse_trivy(check: QACheck) -> list[QAFinding]:
    payload = _json_object(check.output)
    results = payload.get("Results") if payload else None
    if not isinstance(results, list):
        return []
    findings: list[QAFinding] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target", ""))
        for item in result.get("Vulnerabilities") or []:
            findings.append(
                _trivy_finding(item, target, "dependencies", "VulnerabilityID", "Title")
            )
        for item in result.get("Misconfigurations") or []:
            findings.append(_trivy_finding(item, target, "configuration", "ID", "Title"))
        for item in result.get("Secrets") or []:
            findings.append(_trivy_finding(item, target, "secrets", "RuleID", "Title"))
    return findings


def _trivy_finding(
    item: object,
    target: str,
    category: QAFindingCategory,
    id_key: str,
    title_key: str,
) -> QAFinding:
    entry = item if isinstance(item, dict) else {}
    identifier = str(entry.get(id_key, "?"))
    return QAFinding(
        id=f"trivy-{identifier}-{target}",
        title=str(entry.get(title_key) or entry.get("Message") or identifier)[:200],
        severity=severity_of(entry.get("Severity")),
        category=category,
        source="trivy",
        location=str(entry.get("PkgName") or entry.get("Target") or target),
        line=entry.get("StartLine") if isinstance(entry.get("StartLine"), int) else None,
        detail=str(entry.get("Description") or entry.get("Message") or "")[:600],
        remediation=str(entry.get("Resolution") or entry.get("FixedVersion") or ""),
        reference=identifier,
        confidence="high",
    )


_PARSERS: dict[str, Callable[[QACheck], list[QAFinding]]] = {
    "python-sast": _parse_bandit,
    "python-audit": _parse_pip_audit,
    "js-audit": _parse_npm_audit,
    "semgrep": _parse_semgrep,
    "osv-scan": _parse_osv,
    "secret-scan": _parse_gitleaks,
    "rust-audit": _parse_cargo_audit,
    "trivy-scan": _parse_trivy,
}


# ---------------------------------------------------------------- utilities


def _json_value(output: str) -> Any:
    """Parse JSON that a CLI may have prefixed with progress text."""
    text = output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _sub(value: object, key: str) -> dict[str, Any]:
    """A nested object under ``key``, or an empty one.

    Scanner JSON is not a contract; a missing or wrongly-typed nesting level has
    to read as "no data here" rather than raise inside a parser.
    """
    if isinstance(value, dict):
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    return {}


def _json_object(output: str) -> dict[str, Any]:
    value = _json_value(output)
    return value if isinstance(value, dict) else {}


def _npm_remediation(name: str, fix: object) -> str:
    """Say what `npm audit fix` would actually do, including the breaking case."""
    if fix is True:
        return f"`npm audit fix` resolves {name} without a breaking change."
    if isinstance(fix, dict):
        target = f"{fix.get('name', name)}@{fix.get('version', '?')}"
        if fix.get("isSemVerMajor"):
            return f"Upgrade to {target} — a major version bump, so review the changelog first."
        return f"Upgrade to {target}."
    return f"No automatic fix is published for {name}; assess reachability or replace the package."


def _confidence(value: object) -> str:
    text = str(value or "").casefold()
    if text.startswith("high"):
        return "high"
    if text.startswith("low"):
        return "low"
    return "medium"


def _from_cvss(score: float) -> QASeverity:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "info"


def _advisory_severity(identifier: str, description: object) -> QASeverity:
    """Guess a severity when the advisory record does not carry one.

    Ecosystem advisory feeds are inconsistent about publishing a severity. The
    identifier prefix and the wording of the summary are weak signals, but they
    beat filing every unrated advisory at the same level.
    """
    text = f"{identifier} {description or ''}".casefold()
    if any(
        word in text for word in ("remote code execution", "rce", "deserializ", "sandbox escape")
    ):
        return "critical"
    if any(word in text for word in ("injection", "traversal", "authentication bypass", "xss")):
        return "high"
    return "medium"
