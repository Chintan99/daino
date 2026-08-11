"""Parallel, read-only quality assurance for an entire project."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vasuki.agents import TeamRunner
from vasuki.agents.tool_schemas import QA_TOOL_SPECS
from vasuki.application.context import ProjectContext
from vasuki.application.mission_service import MissionApplicationService
from vasuki.config.models import SecurityConfig
from vasuki.model_router import ModelRole
from vasuki.prompts import QA_REVIEW_SYSTEM
from vasuki.schemas import (
    ProjectMode,
    QAAgentAction,
    QACheck,
    QAReport,
    QASpecialist,
    TeamMember,
    TeamMemberOutcome,
    TeamPlan,
)
from vasuki.security.commands import CommandGate
from vasuki.tools.commands import ApprovalCallback, CommandRunner
from vasuki.utils.ids import new_id

QAUpdateCallback = Callable[[QAReport], None]
_REPORT_OUTPUT_LIMIT = 6_000


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


def discover_checks(root: Path, profile: QAProjectProfile) -> list[QACheck]:
    """Build a transparent command list; unavailable optional tools are skipped."""
    checks: list[QACheck] = []
    python_project = any(
        (root / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.cfg", "setup.py")
    )
    if python_project:
        checks.append(
            QACheck(
                id="python-syntax",
                label="Python syntax",
                category="quality",
                command=shlex.join(["python", "-m", "compileall", "-q", *_python_targets(root)]),
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

    if profile.package_manager:
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
        if _has_js_lock(root):
            checks.append(
                QACheck(
                    id="js-audit",
                    label="JavaScript dependency vulnerabilities",
                    category="dependencies",
                    command=_package_audit_command(profile.package_manager),
                    network_required=True,
                )
            )

    checks.append(_playwright_check(root, profile))
    _ecosystem_checks(root, checks)
    return _unique_checks(checks)


def specialist_plan(profile: QAProjectProfile) -> TeamPlan:
    """Return a fixed, auditable roster rather than asking a model to invent QA scope."""
    members = [
        _member(
            "architecture",
            "Architecture",
            "architect",
            "Review module boundaries, data flow, coupling, layering, scalability, error "
            "boundaries, and whether the implementation matches its documented architecture.",
        ),
        _member(
            "security",
            "Security",
            "reviewer",
            "Audit authentication, authorization, input handling, injection paths, secrets, "
            "unsafe execution, sensitive data exposure, and dependency evidence.",
        ),
        _member(
            "code-quality",
            "Code quality",
            "reviewer",
            "Audit correctness, maintainability, error handling, concurrency, typing, dead code, "
            "test quality, and high-risk untested paths across the repository.",
        ),
    ]
    if profile.frontend:
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
    if profile.backend:
        members.append(
            _member(
                "backend",
                "Backend",
                "reviewer",
                "Review API contracts, validation, authorization, persistence and transaction "
                "behavior, concurrency, failure handling, performance, and backend test coverage.",
            )
        )
    reviewer_ids = [member.id for member in members]
    members.append(
        TeamMember(
            id="qa-summary",
            role="summarizer",
            objective=(
                "Synthesize all specialist reports and automated evidence. Deduplicate findings, "
                "resolve conflicts, and produce: overall assessment; release blockers; findings "
                "ordered by severity; quick wins; and missing evidence. Preserve exact file and "
                "line references. Do not claim that a completed review means the code passed."
            ),
            read_only=True,
            dependencies=reviewer_ids,
        )
    )
    return TeamPlan(summary="Comprehensive parallel repository QA", members=members)


class QAApplicationService:
    """Run deterministic checks and a parallel team of read-only reviewers."""

    def __init__(
        self,
        context: ProjectContext,
        missions: MissionApplicationService | None = None,
    ) -> None:
        self.context = context
        self.missions = missions or MissionApplicationService(context)

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
        profile_override: str = "",
        approve: ApprovalCallback | None = None,
        on_update: QAUpdateCallback | None = None,
    ) -> QAReport:
        profile = inspect_project(self.context.root)
        plan = specialist_plan(profile)
        report = QAReport(
            id=new_id("qa"),
            status="running",
            started_at=datetime.now(UTC),
            project_root=str(self.context.root.resolve()),
            project_profile=list(profile.labels),
            checks=discover_checks(self.context.root, profile),
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
        mission = self.missions.core.create("Comprehensive repository QA", ProjectMode.DIRECT)
        report.mission_id = mission.id
        self.missions.core._update_mission(mission.id, status="running")
        self._notify(report, on_update)
        try:
            await self._run_checks(report, approve, on_update)
            await self._run_specialists(report, plan, profile_override, on_update)
            report.status = "completed"
            report.finished_at = datetime.now(UTC)
            if not report.summary:
                report.summary = _fallback_summary(report)
            self.missions.core._update_mission(mission.id, status="completed")
        except asyncio.CancelledError:
            report.status = "cancelled"
            report.finished_at = datetime.now(UTC)
            report.summary = "QA run cancelled; completed evidence is preserved."
            self.missions.core._update_mission(
                mission.id, status="cancelled", failure="Cancelled by user"
            )
            self._save(report)
            self._notify(report, on_update)
            raise
        except Exception as exc:
            report.status = "failed"
            report.finished_at = datetime.now(UTC)
            report.summary = f"QA orchestration failed: {type(exc).__name__}: {exc}"
            self.missions.core._update_mission(mission.id, status="failed", failure=report.summary)
        self._save(report)
        self._notify(report, on_update)
        return report

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
            "Perform comprehensive read-only quality assurance.\n\n"
            f"Detected project profile: {', '.join(report.project_profile)}\n\n"
            f"Automated evidence:\n{evidence or '- No automated checks were applicable.'}"
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
            self._notify(report, on_update)

        outcome = await TeamRunner(
            gateway,
            self.context.root,
            max_steps=12,
            system=QA_REVIEW_SYSTEM,
            tools=QA_TOOL_SPECS,
            action_schema=QAAgentAction,
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
        return self.context.root / ".vasuki" / "qa"

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
        "security": "Security",
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


def _python_targets(root: Path) -> list[str]:
    """Avoid compiling virtual environments, dependencies, and generated output."""
    ignored = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
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
    """Point pip-audit at project inputs instead of Vasuki's own environment."""
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


def _ecosystem_checks(root: Path, checks: list[QACheck]) -> None:
    if (root / "Cargo.toml").exists():
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
    if (root / "Cargo.lock").exists():
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
    if check.category == "dependencies":
        parsed = _dependency_summary(output)
        if parsed:
            return parsed
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = lines[-1][:_REPORT_OUTPUT_LIMIT] if lines else ""
    if success:
        return detail or "Passed."
    return detail or "Command exited unsuccessfully."


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
    failed = [item.label for item in report.checks if item.status == "failed"]
    skipped = [item.label for item in report.checks if item.status == "skipped"]
    completed = [item.label for item in report.specialists if item.status == "passed"]
    lines = ["# QA report", "", "QA run completed.", ""]
    lines.append(f"- Automated failures: {', '.join(failed) if failed else 'none'}.")
    lines.append(f"- Specialists completed: {', '.join(completed) if completed else 'none'}.")
    if skipped:
        lines.append(f"- Skipped checks: {', '.join(skipped)}.")
    lines.extend(["", "## Automated evidence", "", _automated_evidence(report)])
    return "\n".join(lines).strip()


def _automated_evidence(report: QAReport) -> str:
    """Bound command evidence before sharing it with agents or the Markdown view."""
    lines: list[str] = []
    for item in report.checks:
        lines.append(f"- {item.label}: {item.status.upper()} — {item.summary or 'no summary'}")
        if item.output and (item.status == "failed" or item.category == "dependencies"):
            excerpt = item.output[:2_000].replace("\n", "\n    ")
            lines.append(f"    Output (untrusted):\n    {excerpt}")
    return "\n".join(lines) or "- No automated checks were applicable."
