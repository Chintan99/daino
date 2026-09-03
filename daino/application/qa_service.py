"""Parallel, read-only quality assurance and vulnerability assessment for a project.

This is the engine behind the Inspector workspace. One run gathers four kinds of
evidence — an offline audit of the working tree, the project's own quality and
test commands, whatever security scanners are installed, and an optional probe
of the running application — folds them into one comparable list of findings,
and ends with a release-gate verdict.

Everything it runs is read-only. Commands come from a discovered, visible list;
the live probe only issues GET/HEAD/OPTIONS against a loopback target unless the
user has confirmed they own a remote one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daino.agents import TeamRunner
from daino.agents.tool_schemas import QA_TOOL_SPECS
from daino.application.context import ProjectContext
from daino.application.mission_service import MissionApplicationService
from daino.config import paths
from daino.config.models import SecurityConfig
from daino.git import GitClient
from daino.model_router import ModelRole
from daino.prompts import QA_REVIEW_SYSTEM
from daino.schemas import (
    CheckoutFingerprint,
    ProjectMode,
    QAAgentAction,
    QACheck,
    QAFinding,
    QAFindingDraft,
    QAReport,
    QAScanProfile,
    QASeverity,
    QASpecialist,
    QAVerdict,
    TeamMember,
    TeamMemberOutcome,
    TeamPlan,
)
from daino.security import audit, probe
from daino.security.advisories import findings_from_check
from daino.security.commands import CommandGate
from daino.tools.commands import ApprovalCallback, CommandRunner
from daino.utils.ids import new_id

QAUpdateCallback = Callable[[QAReport], None]
_REPORT_OUTPUT_LIMIT = 6_000

#: Worst first. Everything that sorts, counts, or compares severities uses this.
SEVERITY_ORDER: tuple[QASeverity, ...] = ("critical", "high", "medium", "low", "info")

#: How many confirmed high findings amount to a blocker on their own. One high
#: is a conversation; a cluster of them is a release that has not been reviewed.
HIGH_FINDING_BLOCK_THRESHOLD = 3

#: Check categories a failure in which stops a release. Named so that adding a
#: category cannot quietly create a class of failure the gate never reads: a
#: Playwright run is filed under "browser", and a failing one is a failing test.
BLOCKING_CHECK_CATEGORIES: frozenset[str] = frozenset({"tests", "browser"})


@dataclass(frozen=True, slots=True)
class QAProjectProfile:
    """Capabilities that decide which specialists and checks are applicable."""

    labels: tuple[str, ...]
    frontend: bool
    backend: bool
    playwright: bool
    package_manager: str = ""
    package_scripts: tuple[str, ...] = ()
    python_playwright: bool = False


def inspect_project(root: Path) -> QAProjectProfile:
    """Detect QA-relevant stacks without executing project code."""
    package = _package_json(root)
    dependencies = {
        **_string_dict(package.get("dependencies")),
        **_string_dict(package.get("devDependencies")),
    }
    scripts = tuple(sorted(_string_dict(package.get("scripts"))))
    package_manager = _package_manager(root) if package else ""
    source_names = {path.name.casefold() for path in root.iterdir()} if root.exists() else set()
    frontend_markers = {
        "@angular/core",
        "@sveltejs/kit",
        "astro",
        "next",
        "nuxt",
        "react",
        "svelte",
        "vite",
        "vue",
    }
    backend_markers = {
        "@nestjs/core",
        "express",
        "fastify",
        "hapi",
        "koa",
        "next",
    }
    frontend = bool(frontend_markers & dependencies.keys()) or bool(
        {"index.html", "vite.config.js", "vite.config.ts"} & source_names
    )
    backend = bool(backend_markers & dependencies.keys()) or any(
        (root / marker).exists()
        for marker in (
            "pyproject.toml",
            "requirements.txt",
            "go.mod",
            "Cargo.toml",
            "composer.json",
            "backend",
            "server",
        )
    )
    python_config = _config_text(root)
    python_playwright = "pytest-playwright" in python_config
    playwright = (
        "@playwright/test" in dependencies
        or any(root.glob("playwright.config.*"))
        or python_playwright
    )
    labels: list[str] = []
    if frontend:
        labels.append("frontend")
    if backend:
        labels.append("backend")
    if package_manager:
        labels.append(package_manager)
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        labels.append("python")
    if (root / "go.mod").exists():
        labels.append("go")
    if (root / "Cargo.toml").exists():
        labels.append("rust")
    if playwright:
        labels.append("playwright")
    return QAProjectProfile(
        labels=tuple(dict.fromkeys(labels)) or ("general",),
        frontend=frontend,
        backend=backend,
        playwright=playwright,
        package_manager=package_manager,
        package_scripts=scripts,
        python_playwright=python_playwright,
    )


def discover_checks(
    root: Path,
    profile: QAProjectProfile,
    scan_profile: QAScanProfile = "full",
) -> list[QACheck]:
    """Build a transparent command list; unavailable optional tools are skipped.

    ``scan_profile`` decides which halves are worth running: a security-only
    inspection has no reason to rebuild the project, and a quality-only one has
    no reason to download a rule pack.
    """
    checks: list[QACheck] = []
    quality = scan_profile in {"full", "quality"}
    security = scan_profile in {"full", "security"}
    python_project = any(
        (root / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.cfg", "setup.py")
    )
    if python_project and quality:
        checks.append(
            QACheck(
                id="python-syntax",
                label="Python syntax",
                category="quality",
                command=shlex.join(
                    [_python_executable(root), "-m", "compileall", "-q", *_python_targets(root)]
                ),
            )
        )
        config_text = _config_text(root)
        _optional_command(
            checks,
            root,
            tool="ruff",
            arguments="check .",
            check_id="python-lint",
            label="Python lint",
            category="quality",
            configured="ruff" in config_text,
        )
        _optional_command(
            checks,
            root,
            tool="mypy",
            arguments=".",
            check_id="python-types",
            label="Python types",
            category="quality",
            configured="mypy" in config_text,
        )
        if (root / "tests").is_dir():
            _optional_command(
                checks,
                root,
                tool="pytest",
                arguments="-q",
                check_id="python-tests",
                label="Python tests",
                category="tests",
                configured=True,
            )

    if python_project and security:
        manifests = any(
            (root / name).exists()
            for name in (
                "requirements.txt",
                "requirements-dev.txt",
                "pyproject.toml",
                "poetry.lock",
                "uv.lock",
            )
        )
        _optional_command(
            checks,
            root,
            tool="pip-audit",
            arguments=_python_audit_arguments(root),
            check_id="python-audit",
            label="Python dependency vulnerabilities",
            category="dependencies",
            configured=manifests,
            network_required=True,
        )
        _optional_command(
            checks,
            root,
            tool="bandit",
            arguments=shlex.join(["-q", "-f", "json", "-r", *_python_targets(root)]),
            check_id="python-sast",
            label="Python static security analysis",
            category="security",
            configured=True,
        )

    if profile.package_manager and quality:
        selected_scripts = [
            name
            for name in ("lint", "typecheck", "check", "test", "build")
            if name in profile.package_scripts
        ]
        for name in selected_scripts:
            category = "tests" if name == "test" else "quality"
            checks.append(
                QACheck(
                    id=f"js-{name}",
                    label=f"JavaScript {name}",
                    category=category,
                    command=_package_script_command(profile.package_manager, name),
                )
            )

    if profile.package_manager and security and _has_js_lock(root):
        checks.append(
            QACheck(
                id="js-audit",
                label="JavaScript dependency vulnerabilities",
                category="dependencies",
                command=_package_audit_command(profile.package_manager),
                network_required=True,
            )
        )

    if quality:
        checks.append(_playwright_check(root, profile))
    _ecosystem_checks(root, checks, quality=quality, security=security)
    if security:
        _security_scanner_checks(root, checks)
    return _unique_checks(checks)


def _security_scanner_checks(root: Path, checks: list[QACheck]) -> None:
    """Add the ecosystem-independent scanners, when the host actually has them.

    None of these are dependencies of Daino. A team that has installed semgrep
    or trivy gets their depth; a team that has not still gets the built-in audit
    and sees exactly which scanner was missing.
    """
    _optional_command(
        checks,
        root,
        tool="gitleaks",
        arguments="detect --no-banner --redact --report-format json --report-path /dev/stdout",
        check_id="secret-scan",
        label="Secret scan (git history)",
        category="security",
        configured=(root / ".git").exists(),
    )
    _optional_command(
        checks,
        root,
        tool="semgrep",
        arguments="--config auto --json --quiet --error .",
        check_id="semgrep",
        label="Semgrep static analysis",
        category="security",
        configured=True,
        network_required=True,
    )
    _optional_command(
        checks,
        root,
        tool="osv-scanner",
        arguments="--format json --recursive .",
        check_id="osv-scan",
        label="Open-source vulnerability scan",
        category="dependencies",
        configured=True,
        network_required=True,
    )
    _optional_command(
        checks,
        root,
        tool="trivy",
        arguments="fs --scanners vuln,secret,misconfig --format json --quiet .",
        check_id="trivy-scan",
        label="Trivy filesystem scan",
        category="security",
        configured=True,
        network_required=True,
    )


def specialist_plan(
    profile: QAProjectProfile,
    scan_profile: QAScanProfile = "full",
) -> TeamPlan:
    """Return a fixed, auditable roster rather than asking a model to invent QA scope.

    The roster is the expensive part of an inspection, so ``scan_profile``
    trims it: a security-only run does not pay an architecture reviewer, and a
    quality-only run does not pay a threat modeller.
    """
    quality = scan_profile in {"full", "quality"}
    security = scan_profile in {"full", "security"}
    members: list[TeamMember] = []
    if quality:
        members.append(
            _member(
                "architecture",
                "Architecture",
                "architect",
                "Review module boundaries, data flow, coupling, layering, scalability, error "
                "boundaries, and whether the implementation matches its documented architecture.",
            )
        )
        members.append(
            _member(
                "code-quality",
                "Code quality",
                "reviewer",
                "Audit correctness, maintainability, error handling, concurrency, typing, dead "
                "code, test quality, and high-risk untested paths across the repository.",
            )
        )
    if security:
        members.append(
            _member(
                "security",
                "Application security",
                "reviewer",
                "Audit exploitable weaknesses against the OWASP Top 10: authentication and "
                "session handling, authorization and object-level access control, injection "
                "(SQL, command, template, deserialization), SSRF, path traversal, unsafe "
                "redirects, cryptography, and sensitive data exposure. For each issue give the "
                "attack precondition, the concrete exploitation path, the CWE, and the fix. "
                "Triage the supplied scanner findings: say which are exploitable here and which "
                "are false positives, and why.",
            )
        )
        members.append(
            _member(
                "threat-model",
                "Threat model",
                "architect",
                "Map the attack surface before release: entry points (routes, queues, webhooks, "
                "CLI, file uploads), trust boundaries, the authentication and authorization "
                "matrix, what an unauthenticated caller can reach, what a low-privilege user can "
                "escalate to, and where tenant or user data can cross a boundary. Name the "
                "highest-risk paths and what evidence would confirm or clear each one.",
            )
        )
        members.append(
            _member(
                "supply-chain",
                "Supply chain & deployment",
                "reviewer",
                "Audit everything around the code: dependency and lockfile hygiene, CI/CD "
                "workflow permissions and untrusted-input handling, container and IaC hardening, "
                "secret management and rotation, logging that could leak credentials, and the "
                "production configuration defaults the repository ships with.",
            )
        )
    if profile.frontend and quality:
        members.append(
            _member(
                "frontend",
                "Frontend",
                "reviewer",
                "Review frontend state and data flow, rendering correctness, performance, client "
                "security, accessibility, responsive behavior, and test coverage.",
            )
        )
        members.append(
            _member(
                "ui-browser",
                "UI / Playwright",
                "tester",
                "Review UI usability and accessibility and interpret the supplied Playwright "
                "evidence. Inspect routes, forms, loading/error states, keyboard use, and mobile "
                "behavior; distinguish browser-tested facts from static-review risks.",
            )
        )
    if profile.backend and quality:
        members.append(
            _member(
                "backend",
                "Backend",
                "reviewer",
                "Review API contracts, validation, authorization, persistence and transaction "
                "behavior, concurrency, failure handling, performance, and backend test coverage.",
            )
        )
    reviewer_ids = sorted(member.id for member in members)
    members.append(
        TeamMember(
            id="qa-summary",
            role="summarizer",
            objective=(
                "Synthesize all specialist reports, scanner findings, and automated evidence into "
                "one pre-release decision. Deduplicate findings, resolve conflicts, and produce: "
                "a ship / do-not-ship recommendation with its reasons; release blockers; "
                "remaining findings ordered by severity with CWE where known; quick wins; and "
                "the evidence that is still missing. Preserve exact file and line references. Do "
                "not claim that a completed review means the code passed."
            ),
            read_only=True,
            dependencies=reviewer_ids,
        )
    )
    summary = {
        "full": "Comprehensive repository QA and vulnerability assessment",
        "quality": "Comprehensive parallel repository QA",
        "security": "Repository vulnerability assessment",
    }[scan_profile]
    return TeamPlan(summary=summary, members=members)


class QAApplicationService:
    """Run deterministic checks and a parallel team of read-only reviewers."""

    def __init__(
        self,
        context: ProjectContext,
        missions: MissionApplicationService | None = None,
    ) -> None:
        self.context = context
        self.missions = missions or MissionApplicationService(context)
        self.git = GitClient(context.root)

    def checkout(self) -> CheckoutFingerprint:
        """The working tree as it is right now, for pinning or comparing."""
        return CheckoutFingerprint.model_validate(self.git.checkout_fingerprint())

    def is_current(self, report: QAReport | None) -> bool:
        """Whether ``report``'s verdict still describes the working tree.

        A report from before the fingerprint existed has no digest to compare,
        and an unpinnable one (no Git) never gets a digest either. Both are
        reported as not current: an unverifiable clearance is not a clearance.
        """
        if report is None or not report.checkout.digest:
            return False
        return report.checkout.digest == self.checkout().digest

    def latest(self) -> QAReport | None:
        return self._read_report(self._report_directory / "latest.json")

    def history(self, limit: int = 50) -> list[QAReport]:
        """Return this repository's saved reports, newest first."""
        if limit <= 0 or not self._report_directory.is_dir():
            return []
        reports = [
            report
            for path in self._report_directory.glob("qa-*.json")
            if (report := self._read_report(path)) is not None
        ]
        reports.sort(key=lambda report: report.started_at, reverse=True)
        return reports[:limit]

    def load(self, report_id: str) -> QAReport | None:
        """Load one report without allowing paths outside the repository store."""
        if not report_id.startswith("qa-") or Path(report_id).name != report_id:
            return None
        report = self._read_report(self._report_directory / f"{report_id}.json")
        return report if report is not None and report.id == report_id else None

    async def run(
        self,
        *,
        scan_profile: QAScanProfile = "full",
        target_url: str = "",
        authorize_remote_target: bool = False,
        profile_override: str = "",
        approve: ApprovalCallback | None = None,
        on_update: QAUpdateCallback | None = None,
    ) -> QAReport:
        """Run one inspection end to end and return its persisted report.

        ``target_url`` opts into the live probe: when the caller has an app
        running (the Inspector's Live view starts one), the inspection also
        looks at what that app actually returns. ``authorize_remote_target`` is
        the caller's assertion that a non-loopback target belongs to the user.
        """
        profile = inspect_project(self.context.root)
        plan = specialist_plan(profile, scan_profile)
        checks = discover_checks(self.context.root, profile, scan_profile)
        if scan_profile in {"full", "security"}:
            checks.insert(0, _static_audit_check())
            if target_url:
                checks.append(_live_probe_check())
        report = QAReport(
            id=new_id("qa"),
            status="running",
            started_at=datetime.now(UTC),
            project_root=str(self.context.root.resolve()),
            project_profile=list(profile.labels),
            scan_profile=scan_profile,
            target_url=target_url,
            checks=checks,
            checkout=self.checkout(),
            specialists=[
                QASpecialist(
                    id=member.id,
                    label=_specialist_label(member.id),
                    role=member.role,
                    objective=member.objective,
                )
                for member in plan.members
            ],
        )
        mission = self.missions.core.create(_MISSION_TITLES[scan_profile], ProjectMode.DIRECT)
        report.mission_id = mission.id
        self.missions.core._update_mission(mission.id, status="running")
        self._notify(report, on_update)
        try:
            # The offline audit needs nothing and finishes in seconds, so it
            # runs first: the workspace has real findings on screen while the
            # slower commands and reviewers are still going.
            await self._run_static_audit(report, on_update)
            await self._run_checks(report, approve, on_update)
            await self._run_live_probe(report, authorize_remote_target, on_update)
            self._collect_check_findings(report)
            self._apply_gate(report)
            self._notify(report, on_update)
            await self._run_specialists(report, plan, profile_override, on_update)
            report.status = "completed"
            report.finished_at = datetime.now(UTC)
            if not report.summary:
                report.summary = _fallback_summary(report)
            self.missions.core._update_mission(mission.id, status="completed")
        except asyncio.CancelledError:
            report.status = "cancelled"
            report.finished_at = datetime.now(UTC)
            report.summary = "Inspection cancelled; completed evidence is preserved."
            report.verdict = "unknown"
            report.gate_reasons = ["The inspection was cancelled before it finished."]
            self.missions.core._update_mission(
                mission.id, status="cancelled", failure="Cancelled by user"
            )
            self._save(report)
            self._notify(report, on_update)
            raise
        except Exception as exc:
            report.status = "failed"
            report.finished_at = datetime.now(UTC)
            report.summary = f"Inspection failed: {type(exc).__name__}: {exc}"
            report.verdict = "unknown"
            report.gate_reasons = [report.summary]
            self.missions.core._update_mission(mission.id, status="failed", failure=report.summary)
        self._apply_gate(report)
        self._save(report)
        self._notify(report, on_update)
        return report

    async def _run_static_audit(
        self,
        report: QAReport,
        on_update: QAUpdateCallback | None,
    ) -> None:
        """Run the built-in offline audit of the working tree.

        It reads every source file, so it goes to a thread; blocking the event
        loop here would freeze the live progress the workspace is rendering.
        """
        check = next((item for item in report.checks if item.id == "static-audit"), None)
        if check is None:
            return
        check.status = "running"
        self._notify(report, on_update)
        started = datetime.now(UTC)
        try:
            findings = await asyncio.to_thread(audit.audit_repository, self.context.root)
        except OSError as exc:
            check.status = "skipped"
            check.summary = f"The working tree could not be read: {exc}"
            self._notify(report, on_update)
            return
        check.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        _absorb(report, findings)
        counts = severity_counts(findings)
        check.status = "failed" if counts["critical"] or counts["high"] else "passed"
        check.summary = _counts_sentence(findings, "No issues matched the built-in rules.")
        check.output = _findings_evidence(findings)
        self._notify(report, on_update)

    async def _run_live_probe(
        self,
        report: QAReport,
        authorized: bool,
        on_update: QAUpdateCallback | None,
    ) -> None:
        """Probe the running application, if the caller supplied one."""
        check = next((item for item in report.checks if item.id == "live-probe"), None)
        if check is None or not report.target_url:
            return
        check.status = "running"
        check.command = f"GET/HEAD/OPTIONS {report.target_url}"
        self._notify(report, on_update)
        started = datetime.now(UTC)
        findings, evidence = await probe.probe_target(report.target_url, authorized=authorized)
        check.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        _absorb(report, findings)
        counts = severity_counts(findings)
        check.output = _clip(evidence + "\n\n" + _findings_evidence(findings))
        if evidence.startswith("Refused to probe") or "did not answer" in evidence:
            check.status = "skipped"
            check.summary = evidence.splitlines()[-1][:400]
        else:
            check.status = "failed" if counts["critical"] or counts["high"] else "passed"
            check.summary = _counts_sentence(
                findings, "The running app exposed no weakness the probe looks for."
            )
        self._notify(report, on_update)

    def _collect_check_findings(self, report: QAReport) -> None:
        """Fold every scanner's own output into the shared finding list."""
        for check in report.checks:
            _absorb(report, findings_from_check(check))

    def _apply_gate(self, report: QAReport) -> None:
        report.findings = merge_duplicates(report.findings)
        report.findings.sort(key=_finding_sort_key)
        report.verdict, report.gate_reasons = evaluate_gate(report)

    async def _run_checks(
        self,
        report: QAReport,
        approve: ApprovalCallback | None,
        on_update: QAUpdateCallback | None,
    ) -> None:
        network_checks = [
            item
            for item in report.checks
            if item.command and item.network_required and item.status == "pending"
        ]
        if (
            network_checks
            and self.context.settings.runtime.default == "docker"
            and self.context.settings.runtime.network_access != "allowed"
        ):
            for item in network_checks:
                item.status = "skipped"
                item.summary = (
                    "The Docker runtime has network access disabled; switch to local or allow "
                    "container networking to run this audit."
                )
            network_checks = []
        network_allowed = not self.context.settings.security.require_approval_for_network
        if network_checks and not network_allowed:
            if approve is not None:
                commands = "\n".join(item.command for item in network_checks)
                network_allowed, _ = await approve(
                    f"Run dependency vulnerability scans:\n{commands}",
                    "dependency vulnerability scans require network access",
                )
            if not network_allowed:
                for item in network_checks:
                    item.status = "skipped"
                    item.summary = "Network access was not approved."

        runnable = [
            item
            for item in report.checks
            if item.command
            and item.status == "pending"
            and (not item.network_required or network_allowed)
        ]
        if not runnable:
            self._notify(report, on_update)
            return
        runtime = self.missions.core._runtime(self.context.root)
        allowed = sorted(
            {
                shlex.split(item.command)[0].rsplit("/", 1)[-1]
                for item in runnable
                if shlex.split(item.command)
            }
        )
        security = self.context.settings.security.model_copy(
            update={
                "allowed_commands": sorted(
                    set(self.context.settings.security.allowed_commands) | set(allowed)
                )
            }
        )
        assert isinstance(security, SecurityConfig)
        runner = CommandRunner(
            runtime,
            CommandGate(security),
            runtime_name=self.context.settings.runtime.default,
            approve=approve,
            default_timeout=self.context.settings.runtime.command_timeout_seconds,
        )
        try:
            await runtime.prepare()
        except Exception as exc:
            for item in runnable:
                item.status = "skipped"
                item.summary = f"Runtime unavailable: {exc}"
            self._notify(report, on_update)
            return

        async def execute(check: QACheck) -> None:
            check.status = "running"
            self._notify(report, on_update)
            result = await runner.run(check.command)
            check.duration_seconds = result.duration_seconds
            output = str(
                result.data.get("stdout") or result.data.get("stderr") or result.error or ""
            )
            check.output = _clip(output)
            check.status = "passed" if result.success else "failed"
            check.summary = _check_summary(check, output, result.success)
            self._notify(report, on_update)

        try:
            await asyncio.gather(*(execute(item) for item in runnable))
        finally:
            await runtime.cleanup()

    async def _run_specialists(
        self,
        report: QAReport,
        plan: TeamPlan,
        profile_override: str,
        on_update: QAUpdateCallback | None,
    ) -> None:
        if not self.missions.core._role_available(ModelRole.REVIEWER, profile_override):
            for item in report.specialists:
                item.status = "skipped"
                item.summary = "No QA-capable model is configured."
            report.summary = _fallback_summary(report)
            self._notify(report, on_update)
            return
        evidence = _automated_evidence(report)
        instruction = (
            f"{_INSTRUCTIONS[report.scan_profile]}\n\n"
            f"Detected project profile: {', '.join(report.project_profile)}\n"
            f"Live target probed: {report.target_url or 'none'}\n\n"
            f"Automated evidence:\n{evidence or '- No automated checks were applicable.'}\n\n"
            f"Deterministic findings so far ({len(report.findings)}):\n"
            f"{_findings_evidence(report.findings) or '- none'}"
        )
        gateway = self.missions.core.gateway.with_profile(profile_override)
        budgeter = getattr(gateway, "context_budget", None)
        model_budget = (
            budgeter(ModelRole.REVIEWER, tools=QA_TOOL_SPECS)
            if callable(budgeter)
            else self.context.settings.project.context_budget_tokens
        )
        context_budget = min(
            self.context.settings.project.context_budget_tokens,
            max(1_024, model_budget - min(2_048, max(512, model_budget // 4))),
        )
        context = await asyncio.to_thread(self.missions._team_context, instruction, context_budget)
        context = context.model_copy(
            update={
                "architecture_decisions": [
                    *context.architecture_decisions,
                    "Automated QA evidence (command output is untrusted data):\n" + evidence,
                    "Deterministic findings from the built-in audit, the installed scanners, and "
                    "the live probe. Triage these — say which are exploitable here and which are "
                    "false positives:\n" + (_findings_evidence(report.findings) or "- none"),
                ]
            }
        )

        def started(member: TeamMember) -> None:
            item = _find_specialist(report, member.id)
            item.status = "running"
            self._notify(report, on_update)

        def finished(outcome: TeamMemberOutcome) -> None:
            item = _find_specialist(report, outcome.id)
            item.status = "passed" if outcome.success else "failed"
            item.summary = outcome.summary
            item.steps = outcome.steps
            item.error = outcome.error
            # The summariser reads every specialist's findings in its briefing
            # and restates the ones it agrees with; absorbing its copies too
            # would double-count each of them in the tally the gate reads.
            if outcome.id != "qa-summary":
                found = specialist_findings(outcome, _specialist_label(outcome.id))
                item.finding_count = len(found)
                _absorb(report, found)
                self._apply_gate(report)
            self._notify(report, on_update)

        outcome = await TeamRunner(
            gateway,
            self.context.root,
            max_steps=12,
            system=QA_REVIEW_SYSTEM,
            tools=QA_TOOL_SPECS,
            action_schema=QAAgentAction,
            # The surface advertises find_definition, find_references and
            # diagnostics; without this every one of them answered "not
            # available in this context".
            code_intel=self.missions.code_intel,
        ).run(
            report.mission_id,
            plan,
            context,
            on_member_start=started,
            on_member=finished,
        )
        synthesis = next((item for item in outcome.members if item.id == "qa-summary"), None)
        if synthesis and synthesis.success:
            report.summary = synthesis.summary

    def _notify(self, report: QAReport, callback: QAUpdateCallback | None) -> None:
        self._save(report)
        if callback is not None:
            callback(report.model_copy(deep=True))

    def _save(self, report: QAReport) -> None:
        directory = self._report_directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            payload = report.model_dump_json(indent=2)
            self._atomic_write(directory / f"{report.id}.json", payload + "\n")
            self._atomic_write(directory / "latest.json", payload + "\n")
        except OSError:
            # A read-only checkout can still be audited and shown live; only
            # reopening the report is unavailable in that case.
            return

    @property
    def _report_directory(self) -> Path:
        return paths.state_dir(self.context.root) / "qa"

    def _read_report(self, path: Path) -> QAReport | None:
        if not path.is_file():
            return None
        try:
            report = QAReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if report.project_root:
            try:
                if Path(report.project_root).resolve() != self.context.root.resolve():
                    return None
            except OSError:
                return None
        return report

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)


_MISSION_TITLES: dict[QAScanProfile, str] = {
    "full": "Comprehensive repository QA and vulnerability assessment",
    "quality": "Comprehensive repository QA",
    "security": "Repository vulnerability assessment",
}


def _static_audit_check() -> QACheck:
    """The built-in audit, shown as a check so it is visible even when it passes."""
    return QACheck(
        id="static-audit",
        label="Built-in security audit",
        category="security",
        command="",
        summary="",
    )


def _live_probe_check() -> QACheck:
    return QACheck(
        id="live-probe",
        label="Live application probe",
        category="runtime",
        command="",
        summary="",
    )


def specialist_findings(outcome: TeamMemberOutcome, label: str = "") -> list[QAFinding]:
    """Turn one reviewer's reported findings into records the gate can read.

    ``id`` and ``source`` are assigned here rather than taken from the model.
    An id it chose could collide with another specialist's, silently replacing
    a real finding through :func:`_absorb`'s de-duplication; a source it chose
    could name a scanner, so a model's opinion would be filed as a tool's
    measurement.
    """
    source = label or outcome.id
    findings: list[QAFinding] = []
    for index, draft in enumerate(outcome.findings, start=1):
        title = draft.title.strip()
        if not title:
            continue
        findings.append(
            QAFinding(
                id=f"{outcome.id}:{index:03d}:{_finding_slug(draft)}",
                title=title,
                severity=draft.severity,
                category=draft.category,
                source=source,
                location=draft.location.strip(),
                line=draft.line,
                detail=draft.detail.strip(),
                remediation=draft.remediation.strip(),
                cwe=draft.cwe.strip(),
                reference=draft.reference.strip(),
                confidence=draft.confidence,
            )
        )
    return findings


def _finding_slug(draft: QAFindingDraft) -> str:
    """A short, stable tail for a finding id, from what the finding is about."""
    seed = f"{draft.location}:{draft.line or 0}:{draft.title}".casefold()
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]


def _absorb(report: QAReport, findings: list[QAFinding]) -> None:
    """Add findings the report does not already have, keyed by identity."""
    known = {item.id for item in report.findings}
    report.findings.extend(item for item in findings if item.id not in known)


def merge_duplicates(findings: list[QAFinding]) -> list[QAFinding]:
    """Collapse one weakness reported by more than one source into one finding.

    The built-in audit and an installed scanner will both see ``shell=True`` on
    line 4 of the same file. Left alone that reads as two problems and counts
    twice in the tally the release gate reads, so an identical
    file/line/CWE triple becomes a single finding that credits every source
    that saw it. Findings without all three — a live-probe result, a
    dependency advisory — are never merged, because their identity is not a
    source location.
    """
    Key = tuple[str, int, str]
    groups: dict[Key, list[QAFinding]] = {}
    order: list[Key | QAFinding] = []
    for finding in findings:
        if not (finding.location and finding.line is not None and finding.cwe):
            order.append(finding)
            continue
        key: Key = (finding.location.casefold(), finding.line, finding.cwe)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)

    merged: list[QAFinding] = []
    for item in order:
        if isinstance(item, QAFinding):
            merged.append(item)
            continue
        group = groups[item]
        best = max(group, key=_finding_rank)
        if len(group) > 1:
            sources = ", ".join(dict.fromkeys(entry.source for entry in group if entry.source))
            best = best.model_copy(update={"source": sources})
        merged.append(best)
    return merged


def _finding_rank(finding: QAFinding) -> tuple[int, int]:
    """Worse severity wins; ties go to the source that is more sure."""
    confidence = {"high": 2, "medium": 1, "low": 0}
    return (
        -SEVERITY_ORDER.index(finding.severity),
        confidence.get(finding.confidence, 1),
    )


def severity_counts(findings: list[QAFinding]) -> dict[str, int]:
    """Count findings by severity, always returning every level."""
    counts: dict[str, int] = dict.fromkeys(SEVERITY_ORDER, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def evaluate_gate(report: QAReport) -> tuple[QAVerdict, list[str]]:
    """Decide whether this repository should be pushed, and say why.

    The gate is deliberately deterministic and stated in full. A verdict a team
    cannot predict is a verdict they will override, so nothing here depends on
    a model's opinion: it reads the findings and the checks, and every reason it
    gives names the evidence behind it.

    Low-confidence findings — a credential shape inside a test fixture, a
    pattern that matched a path the rules cannot fully resolve — never block on
    their own. They still appear in the report.
    """
    if report.status == "cancelled":
        return "unknown", ["The inspection was cancelled before it finished."]
    if report.status == "failed":
        return "unknown", ["The inspection did not complete, so nothing was cleared."]

    confident = [item for item in report.findings if item.confidence != "low"]
    critical = [item for item in confident if item.severity == "critical"]
    high = [item for item in confident if item.severity == "high"]
    medium = [item for item in confident if item.severity == "medium"]
    # A browser test is a test. Playwright is filed under its own category so
    # the workspace can group it, not because a failing end-to-end run is a
    # softer signal than a failing unit run — it is usually a harder one.
    failed_tests = [
        item
        for item in report.checks
        if item.status == "failed" and item.category in BLOCKING_CHECK_CATEGORIES
    ]
    failed_quality = [
        item for item in report.checks if item.status == "failed" and item.category == "quality"
    ]
    failed_runtime = [
        item for item in report.checks if item.status == "failed" and item.category == "runtime"
    ]
    skipped_security = [
        item
        for item in report.checks
        if item.status == "skipped" and item.category in {"security", "dependencies"}
    ]

    # A narrowed scan must never read as a broad clearance.
    scope = _SCOPE_CAVEATS[report.scan_profile]

    blockers: list[str] = []
    if critical:
        blockers.append(f"{len(critical)} critical finding(s): " + _titles(critical))
    if failed_tests:
        blockers.append(
            "the project's own tests failed: " + ", ".join(item.label for item in failed_tests)
        )
    if len(high) >= HIGH_FINDING_BLOCK_THRESHOLD:
        blockers.append(f"{len(high)} high-severity findings: " + _titles(high))
    if blockers:
        return "blocked", [*blockers, *scope]

    warnings: list[str] = []
    if high:
        warnings.append(f"{len(high)} high-severity finding(s): " + _titles(high))
    if medium:
        warnings.append(f"{len(medium)} medium-severity finding(s) to triage.")
    if failed_quality:
        warnings.append(
            "quality checks failed: " + ", ".join(item.label for item in failed_quality)
        )
    if failed_runtime:
        # The probe may have failed because the app was stopped rather than
        # because it is broken, which is why this warns rather than blocks.
        warnings.append(
            "the running application did not answer: "
            + ", ".join(item.label for item in failed_runtime)
        )
    if skipped_security:
        warnings.append(
            "security evidence is incomplete — these did not run: "
            + ", ".join(item.label for item in skipped_security)
        )
    broken_reviewers = [item.label for item in report.specialists if item.status == "failed"]
    if broken_reviewers:
        # A reviewer that errored looked at nothing, which is not the same as
        # a reviewer that looked and found nothing.
        warnings.append("these reviewers did not complete: " + ", ".join(broken_reviewers))
    if warnings:
        return "warn", [*warnings, *_advisory_caveats(report), *scope]

    cleared = [item.label for item in report.checks if item.status == "passed"]
    return "pass", [
        "No critical or high findings, and no failing test, browser, or quality check.",
        f"Evidence gathered from: {', '.join(cleared) or 'the built-in audit only'}.",
        *_advisory_caveats(report),
        *scope,
    ]


def _advisory_caveats(report: QAReport) -> list[str]:
    """Say what the reviewers contributed, and what is only in their prose.

    Their structured findings *are* weighed now — they go through the same
    severity and confidence rules as a scanner's. What still is not weighed is
    everything a reviewer said and did not file as a finding, which is most of
    an architecture review. A badge that stayed silent about that would imply
    the reviewers had nothing further to say.
    """
    reported = [item for item in report.specialists if item.status == "passed"]
    if not reported:
        return []
    filed = sum(item.finding_count for item in reported)
    return [
        "Advisory: "
        + ", ".join(item.label for item in reported)
        + f" reviewed this as well and filed {filed} finding(s), counted above. "
        "The rest of their assessment is prose in the summary and is NOT part of "
        "this verdict — read it before you push."
    ]


#: What a narrowed scan did *not* look at. Stated on every verdict, because a
#: gate that stays silent about its scope is a gate that gets over-trusted.
_SCOPE_CAVEATS: dict[str, list[str]] = {
    "full": [],
    "quality": ["Scope: quality only — no vulnerability assessment was run."],
    "security": ["Scope: security only — the project's own tests and quality checks were not run."],
}


def _titles(findings: list[QAFinding], limit: int = 3) -> str:
    shown = "; ".join(item.title for item in findings[:limit])
    remainder = len(findings) - limit
    return f"{shown}{f'; and {remainder} more' if remainder > 0 else ''}"


def _finding_sort_key(finding: QAFinding) -> tuple[int, int, str]:
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    return (
        SEVERITY_ORDER.index(finding.severity),
        confidence_rank.get(finding.confidence, 1),
        finding.location,
    )


def _counts_sentence(findings: list[QAFinding], empty: str) -> str:
    if not findings:
        return empty
    counts = severity_counts(findings)
    parts = [f"{count} {level}" for level, count in counts.items() if count]
    return f"{len(findings)} finding(s): {', '.join(parts)}."


def _findings_evidence(findings: list[QAFinding], limit: int = 60) -> str:
    """Render findings as the check's own output, so the evidence is inspectable."""
    lines = [
        f"[{item.severity.upper()}] {item.title}"
        + (f" — {item.location}:{item.line}" if item.line else f" — {item.location}")
        + (f" ({item.cwe})" if item.cwe else "")
        for item in findings[:limit]
    ]
    if len(findings) > limit:
        lines.append(f"… and {len(findings) - limit} more.")
    return "\n".join(lines)


def _member(identifier: str, label: str, role: str, objective: str) -> TeamMember:
    return TeamMember(
        id=identifier,
        role=role,
        objective=f"{label} specialist. {objective}",
        read_only=True,
    )


def _specialist_label(identifier: str) -> str:
    return {
        "architecture": "Architecture",
        "security": "Application security",
        "threat-model": "Threat model",
        "supply-chain": "Supply chain & deployment",
        "code-quality": "Code quality",
        "frontend": "Frontend",
        "backend": "Backend",
        "ui-browser": "UI / Playwright",
        "qa-summary": "Consolidated report",
    }.get(identifier, identifier.replace("-", " ").title())


def _find_specialist(report: QAReport, identifier: str) -> QASpecialist:
    return next(item for item in report.specialists if item.id == identifier)


def _package_json(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if any((root / name).exists() for name in ("bun.lock", "bun.lockb")):
        return "bun"
    return "npm"


def _package_script_command(manager: str, name: str) -> str:
    return f"{manager} run {name}"


def _package_audit_command(manager: str) -> str:
    if manager == "bun":
        return "bun audit"
    return f"{manager} audit --json"


def _has_js_lock(root: Path) -> bool:
    return any(
        (root / name).exists()
        for name in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
        )
    )


def _config_text(root: Path) -> str:
    values: list[str] = []
    for name in ("pyproject.toml", "setup.cfg", "tox.ini", ".ruff.toml", "mypy.ini"):
        path = root / name
        if path.exists():
            try:
                values.append(path.read_text(encoding="utf-8", errors="replace").casefold())
            except OSError:
                pass
    return "\n".join(values)


def _python_executable(root: Path) -> str:
    """The interpreter to run the syntax check with.

    Never the bare name ``python``: it does not exist on a modern macOS or on
    most Linux distributions, where the binary is ``python3``. Hardcoding it
    meant the syntax check reported "failed" on every such machine — a false
    quality failure on a project whose syntax was fine, which is exactly the
    kind of finding that teaches people to ignore the panel.

    The project's own virtualenv wins when there is one, so the check runs on
    the version the project targets; otherwise the interpreter Daino itself is
    running under, which is guaranteed to exist.
    """
    for relative in (Path(".venv/bin/python"), Path(".venv/Scripts/python.exe")):
        if (root / relative).is_file():
            return (root / relative).as_posix()
    return sys.executable or "python3"


def _python_targets(root: Path) -> list[str]:
    """Avoid compiling virtual environments, dependencies, and generated output."""
    ignored = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".daino",
        ".vasuki",
        ".venv",
        "build",
        "dist",
        "node_modules",
    }
    targets = [path.name for path in root.glob("*.py")]
    for path in root.iterdir():
        if not path.is_dir() or path.name in ignored or path.name.startswith("."):
            continue
        if next(path.rglob("*.py"), None) is not None:
            targets.append(path.name)
    return sorted(set(targets)) or ["."]


def _python_audit_arguments(root: Path) -> str:
    """Point pip-audit at project inputs instead of Daino's own environment."""
    requirements = next(
        (name for name in ("requirements.txt", "requirements-dev.txt") if (root / name).exists()),
        "",
    )
    if requirements:
        return f"--format json --requirement {requirements}"
    return "--format json ."


def _tool_path(root: Path, name: str) -> str:
    for relative in (Path(".venv/bin") / name, Path("node_modules/.bin") / name):
        if (root / relative).is_file():
            return relative.as_posix()
    bundled = Path(sys.executable).parent / name
    if bundled.is_file():
        return str(bundled)
    return name if shutil.which(name) else ""


def _optional_command(
    checks: list[QACheck],
    root: Path,
    *,
    tool: str,
    arguments: str,
    check_id: str,
    label: str,
    category: str,
    configured: bool,
    network_required: bool = False,
) -> None:
    if not configured:
        return
    executable = _tool_path(root, tool)
    checks.append(
        QACheck(
            id=check_id,
            label=label,
            category=category,
            command=f"{executable} {arguments}" if executable else "",
            status="pending" if executable else "skipped",
            summary="" if executable else f"{tool} is not installed; check was not run.",
            network_required=network_required,
        )
    )


def _playwright_check(root: Path, profile: QAProjectProfile) -> QACheck:
    if not profile.playwright:
        return QACheck(
            id="playwright",
            label="Playwright UI",
            category="browser",
            status="skipped",
            summary="Not applicable: no Playwright dependency or configuration was detected.",
        )
    if profile.python_playwright:
        pytest = _tool_path(root, "pytest")
        targets = [
            path.as_posix()
            for path in (Path("tests/e2e"), Path("tests/browser"), Path("e2e"))
            if (root / path).exists()
        ]
        command = shlex.join([pytest, "-q", *(targets or ["tests"])]) if pytest else ""
        return QACheck(
            id="playwright",
            label="Playwright UI",
            category="browser",
            command=command,
            status="pending" if command else "skipped",
            summary=(
                ""
                if command
                else "pytest-playwright is configured but no pytest runner is available."
            ),
        )
    scripts = set(profile.package_scripts)
    script = next(
        (name for name in ("test:e2e", "e2e", "test:e2e:ci", "playwright") if name in scripts),
        "",
    )
    if script and profile.package_manager:
        command = _package_script_command(profile.package_manager, script)
    else:
        executable = _tool_path(root, "playwright")
        command = f"{executable} test --reporter=line" if executable else ""
    return QACheck(
        id="playwright",
        label="Playwright UI",
        category="browser",
        command=command,
        status="pending" if command else "skipped",
        summary=(
            ""
            if command
            else "Playwright is configured but no local runner or e2e script is available."
        ),
    )


def _ecosystem_checks(
    root: Path,
    checks: list[QACheck],
    *,
    quality: bool = True,
    security: bool = True,
) -> None:
    if (root / "Cargo.toml").exists() and quality:
        cargo = _tool_path(root, "cargo")
        checks.append(
            QACheck(
                id="rust-tests",
                label="Rust tests",
                category="tests",
                command=f"{cargo} test" if cargo else "",
                status="pending" if cargo else "skipped",
                summary="" if cargo else "cargo is not installed.",
            )
        )
    if (root / "Cargo.lock").exists() and security:
        _optional_command(
            checks,
            root,
            tool="cargo-audit",
            arguments="--json",
            check_id="rust-audit",
            label="Rust dependency vulnerabilities",
            category="dependencies",
            configured=True,
            network_required=True,
        )
    if (root / "go.mod").exists():
        if quality:
            go = _tool_path(root, "go")
            checks.append(
                QACheck(
                    id="go-tests",
                    label="Go tests",
                    category="tests",
                    command=f"{go} test ./..." if go else "",
                    status="pending" if go else "skipped",
                    summary="" if go else "go is not installed.",
                )
            )
        if security:
            _optional_command(
                checks,
                root,
                tool="govulncheck",
                arguments="./...",
                check_id="go-audit",
                label="Go dependency vulnerabilities",
                category="dependencies",
                configured=True,
                network_required=True,
            )


def _unique_checks(checks: list[QACheck]) -> list[QACheck]:
    return list({item.id: item for item in checks}.values())


def _clip(value: str) -> str:
    value = value.strip()
    if len(value) <= _REPORT_OUTPUT_LIMIT:
        return value
    half = _REPORT_OUTPUT_LIMIT // 2
    return f"{value[:half]}\n… output trimmed …\n{value[-half:]}"


def _check_summary(check: QACheck, output: str, success: bool) -> str:
    if check.category in {"dependencies", "security"}:
        parsed = _security_summary(check, output)
        if parsed:
            return parsed
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = lines[-1][:_REPORT_OUTPUT_LIMIT] if lines else ""
    if success:
        return detail or "Passed."
    return detail or "Command exited unsuccessfully."


def _security_summary(check: QACheck, output: str) -> str:
    """Prefer a parsed finding count; fall back to the dependency-metadata shape.

    A scanner's exit code says whether it found something, not what. Counting
    the findings we actually parsed is the only summary that stays true when a
    tool changes its exit conventions.
    """
    parsed = findings_from_check(check.model_copy(update={"output": output}))
    if parsed:
        counts = severity_counts(parsed)
        parts = [f"{count} {level}" for level, count in counts.items() if count]
        return f"{len(parsed)} finding(s): {', '.join(parts)}."
    return _dependency_summary(output)


def _dependency_summary(output: str) -> str:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            vulnerable = sum(bool(item.get("vulns")) for item in payload if isinstance(item, dict))
            return f"{vulnerable} vulnerable Python package(s) reported."
        return ""
    dependencies = payload.get("dependencies")
    if isinstance(dependencies, list):
        vulnerable = sum(bool(item.get("vulns")) for item in dependencies if isinstance(item, dict))
        python_vulnerabilities = sum(
            len(item.get("vulns", []))
            for item in dependencies
            if isinstance(item, dict) and isinstance(item.get("vulns"), list)
        )
        return (
            f"{python_vulnerabilities} Python vulnerability finding(s) across "
            f"{vulnerable} package(s)."
        )
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("vulnerabilities"), dict):
        values = metadata["vulnerabilities"]
        reported_total = values.get("total")
        total = (
            reported_total
            if isinstance(reported_total, int)
            else sum(v for v in values.values() if isinstance(v, int))
        )
        return f"{total} JavaScript vulnerability finding(s) reported."
    rust_vulnerabilities = payload.get("vulnerabilities")
    if isinstance(rust_vulnerabilities, dict) and isinstance(
        rust_vulnerabilities.get("list"), list
    ):
        return f"{len(rust_vulnerabilities['list'])} Rust vulnerability finding(s) reported."
    return ""


def _fallback_summary(report: QAReport) -> str:
    """The report written from evidence alone, when no model summarised the run."""
    failed = [item.label for item in report.checks if item.status == "failed"]
    skipped = [item.label for item in report.checks if item.status == "skipped"]
    completed = [item.label for item in report.specialists if item.status == "passed"]
    counts = severity_counts(report.findings)
    lines = [
        "# Inspection report",
        "",
        f"**Release gate: {_VERDICT_HEADLINES[report.verdict]}**",
        "",
    ]
    lines.extend(f"- {reason}" for reason in report.gate_reasons)
    lines.extend(
        [
            "",
            "## Findings by severity",
            "",
            ", ".join(f"{level}: {count}" for level, count in counts.items()) or "none",
        ]
    )
    if report.findings:
        lines.extend(["", "```", _findings_evidence(report.findings, limit=40), "```"])
    lines.extend(
        [
            "",
            "## Checks",
            "",
            f"- Automated failures: {', '.join(failed) if failed else 'none'}.",
            f"- Specialists completed: {', '.join(completed) if completed else 'none'}.",
        ]
    )
    if skipped:
        lines.append(f"- Skipped checks: {', '.join(skipped)}.")
    lines.extend(["", "## Automated evidence", "", _automated_evidence(report)])
    return "\n".join(lines).strip()


_VERDICT_HEADLINES: dict[str, str] = {
    "pass": "PASS — no blocker was found",
    "warn": "REVIEW — findings to triage before pushing",
    "blocked": "BLOCKED — do not push until these are resolved",
    "unknown": "UNKNOWN — the inspection did not finish",
}

_INSTRUCTIONS: dict[str, str] = {
    "full": (
        "Perform comprehensive read-only quality assurance and a vulnerability assessment of "
        "this repository ahead of a production release."
    ),
    "quality": "Perform comprehensive read-only quality assurance.",
    "security": (
        "Perform a read-only vulnerability assessment of this repository ahead of a production "
        "release. Prioritise exploitability over style."
    ),
}


def _automated_evidence(report: QAReport) -> str:
    """Bound command evidence before sharing it with agents or the Markdown view."""
    lines: list[str] = []
    for item in report.checks:
        lines.append(f"- {item.label}: {item.status.upper()} — {item.summary or 'no summary'}")
        if item.output and (item.status == "failed" or item.category == "dependencies"):
            excerpt = item.output[:2_000].replace("\n", "\n    ")
            lines.append(f"    Output (untrusted):\n    {excerpt}")
    return "\n".join(lines) or "- No automated checks were applicable."
