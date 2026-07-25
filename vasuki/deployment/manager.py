"""Transactional Docker Compose deployments with health-gated promotion and rollback."""

from __future__ import annotations

import re
import shlex
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from sqlalchemy import select

from vasuki.config.models import DeploymentTargetConfig, Settings
from vasuki.exceptions import DeploymentError, PolicyDenied
from vasuki.persistence import Database
from vasuki.persistence.models import DeploymentRun
from vasuki.runtimes import LocalRuntime, RemoteSSHRuntime, Runtime
from vasuki.schemas.core import DeploymentPlan, DeploymentRisk
from vasuki.security import PolicyEngine
from vasuki.utils.ids import new_id

SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


class DeploymentManager:
    def __init__(self, root: Path, settings: Settings, database: Database) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.database = database
        self.policy = PolicyEngine(settings.security)

    def target(self, name: str) -> DeploymentTargetConfig:
        try:
            target = self.settings.deployment.targets[name]
        except KeyError as exc:
            raise DeploymentError(f"Unknown deployment target {name}") from exc
        if not SAFE_REMOTE_PATH.fullmatch(target.deployment_path):
            raise DeploymentError("deployment_path contains unsafe characters")
        return target

    def runtime(self, target: DeploymentTargetConfig) -> Runtime:
        if target.type == "ssh":
            return RemoteSSHRuntime(
                target,
                self.policy,
                timeout=self.settings.runtime.command_timeout_seconds,
            )
        return LocalRuntime(
            self.root,
            self.policy,
            timeout=self.settings.runtime.command_timeout_seconds,
            allow_absolute_paths=True,
        )

    async def inspect(self, target_name: str) -> dict[str, Any]:
        target = self.target(target_name)
        runtime = self.runtime(target)
        await runtime.prepare()
        try:
            if target.type == "ssh":
                return await runtime.inspect()
            commands = {
                "docker": "docker version",
                "compose": "docker compose version",
                "containers": "docker ps",
                "ports": "docker ps --format {{.Ports}}",
            }
            result: dict[str, Any] = {}
            for key, command in commands.items():
                output = await runtime.execute(command, approved=True)
                result[key] = {
                    "success": output.succeeded,
                    "output": output.stdout or output.stderr,
                }
            return result
        finally:
            await runtime.cleanup()

    async def create_plan(self, target_name: str) -> DeploymentPlan:
        target = self.target(target_name)
        inspection = await self.inspect(target_name)
        compose = self.root / target.compose_file
        if not compose.exists():
            raise DeploymentError(f"Compose file not found: {target.compose_file}")
        environment_refs: list[str] = []
        text = compose.read_text(encoding="utf-8")
        environment_refs.extend(sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))))
        risk = (
            DeploymentRisk.HIGH
            if target.environment.lower() in {"production", "prod"}
            else DeploymentRisk.MEDIUM
        )
        health_checks = ["All Compose services are running without restart loops"]
        if target.health_url:
            health_checks.append(f"HTTP GET {target.health_url} returns 2xx")
        health_checks.extend(
            f"Configured smoke command succeeds: {command}" for command in target.health_commands
        )
        return DeploymentPlan(
            target=target_name,
            detected_environment=inspection,
            deployment_strategy="versioned docker-compose release",
            required_changes=[
                "Upload an immutable source bundle",
                "Create a versioned release directory",
                "Start Compose services and verify health before promotion",
            ],
            files_to_upload=[target.compose_file, "source release bundle"],
            persistent_volumes=[],
            environment_variables=environment_refs,
            health_checks=health_checks,
            risk_level=risk,
            destructive_actions=[],
            rollback_strategy=[
                "Stop failed release",
                "Restore previous current symlink",
                "Restart previous Compose release",
                "Verify previous health",
            ],
        )

    def _bundle(self, release_id: str) -> Path:
        output = self.root / ".vasuki" / "artifacts" / "deployments" / f"{release_id}.tar.gz"
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as archive:
            for path in self.root.rglob("*"):
                relative = path.relative_to(self.root)
                if (
                    path.is_file()
                    and ".git" not in relative.parts
                    and ".vasuki" not in relative.parts
                    and not path.is_symlink()
                ):
                    archive.add(path, arcname=relative)
        return output

    async def apply(
        self, target_name: str, *, approved: bool, mission_id: str | None = None
    ) -> DeploymentRun:
        target = self.target(target_name)
        decision = self.policy.deployment_decision(target.environment, approved)
        if not decision.allowed:
            raise PolicyDenied("; ".join(decision.reasons))
        plan = await self.create_plan(target_name)
        release_id = new_id("release")
        run = DeploymentRun(
            id=new_id("deployment"),
            target_name=target_name,
            mission_id=mission_id,
            release_id=release_id,
            status="preparing",
            plan=plan.model_dump(mode="json"),
            evidence={},
        )
        with self.database.session() as session:
            session.add(run)
        runtime = self.runtime(target)
        await runtime.prepare()
        base = PurePosixPath(target.deployment_path)
        release = base / "releases" / release_id
        current = base / "current"
        compose_file = shlex.quote(target.compose_file)
        previous = ""
        commands: list[dict[str, Any]] = []
        try:
            prior = await runtime.execute(f"readlink -f {shlex.quote(str(current))}", approved=True)
            previous = prior.stdout.strip()
            run.previous_release = previous or None
            mkdir = await runtime.execute(
                f"mkdir -p {shlex.quote(str(release))} {shlex.quote(str(base / 'shared'))}",
                approved=True,
            )
            commands.append(mkdir.model_dump(mode="json"))
            if not mkdir.succeeded:
                raise DeploymentError(mkdir.stderr)
            bundle = self._bundle(release_id)
            remote_bundle = str(release / "release.tar.gz")
            await runtime.upload(bundle, remote_bundle)
            unpack = await runtime.execute(
                f"tar -xzf {shlex.quote(remote_bundle)} -C {shlex.quote(str(release))}",
                approved=True,
            )
            commands.append(unpack.model_dump(mode="json"))
            if not unpack.succeeded:
                raise DeploymentError(unpack.stderr)
            if previous:
                old_down = await runtime.execute(
                    f"docker compose -f {shlex.quote(previous)}/{compose_file} down",
                    approved=True,
                )
                commands.append(old_down.model_dump(mode="json"))
            up = await runtime.execute(
                f"docker compose -f {shlex.quote(str(release))}/{compose_file} up -d --build",
                approved=True,
            )
            commands.append(up.model_dump(mode="json"))
            if not up.succeeded:
                raise DeploymentError(f"Compose startup failed: {up.stderr}")
            healthy, verification = await self._verify_runtime(runtime, target, release)
            commands.extend(verification)
            if not healthy:
                rollback = await self._rollback_runtime(runtime, target, previous, release)
                commands.extend(rollback)
                raise DeploymentError("New release failed verification and rollback was attempted")
            promote = await runtime.execute(
                f"ln -sfn {shlex.quote(str(release))} {shlex.quote(str(current))}",
                approved=True,
            )
            commands.append(promote.model_dump(mode="json"))
            if not promote.succeeded:
                raise DeploymentError(f"Promotion failed: {promote.stderr}")
            run.status = "healthy"
            run.evidence = {"commands": commands, "release": str(release)}
            self._save_run(run)
            return run
        except Exception as exc:
            run.status = "failed"
            run.evidence = {"commands": commands, "error": str(exc), "previous": previous}
            self._save_run(run)
            raise
        finally:
            await runtime.cleanup()

    async def _verify_runtime(
        self,
        runtime: Runtime,
        target: DeploymentTargetConfig,
        release: PurePosixPath,
    ) -> tuple[bool, list[dict[str, Any]]]:
        compose = shlex.quote(target.compose_file)
        result = await runtime.execute(
            f"docker compose -f {shlex.quote(str(release))}/{compose} ps --format json",
            approved=True,
        )
        evidence = [result.model_dump(mode="json")]
        healthy = result.succeeded and "restarting" not in result.stdout.lower()
        if target.health_url:
            if target.type == "ssh":
                health = await runtime.execute(
                    f"curl --fail --silent --show-error {shlex.quote(target.health_url)}",
                    approved=True,
                )
                evidence.append(health.model_dump(mode="json"))
                healthy = healthy and health.succeeded
            else:
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        response = await client.get(target.health_url)
                        healthy = healthy and response.is_success
                        evidence.append(
                            {"health_url": target.health_url, "status": response.status_code}
                        )
                except httpx.HTTPError as exc:
                    healthy = False
                    evidence.append({"health_url": target.health_url, "error": str(exc)})
        for command in target.health_commands:
            smoke = await runtime.execute(command, approved=True)
            evidence.append(smoke.model_dump(mode="json"))
            healthy = healthy and smoke.succeeded
        return healthy, evidence

    async def _rollback_runtime(
        self,
        runtime: Runtime,
        target: DeploymentTargetConfig,
        previous: str,
        failed_release: PurePosixPath,
    ) -> list[dict[str, Any]]:
        compose = shlex.quote(target.compose_file)
        evidence = []
        stop = await runtime.execute(
            f"docker compose -f {shlex.quote(str(failed_release))}/{compose} down",
            approved=True,
        )
        evidence.append(stop.model_dump(mode="json"))
        if previous:
            current = PurePosixPath(target.deployment_path) / "current"
            restore = await runtime.execute(
                f"ln -sfn {shlex.quote(previous)} {shlex.quote(str(current))}",
                approved=True,
            )
            evidence.append(restore.model_dump(mode="json"))
            restart = await runtime.execute(
                f"docker compose -f {shlex.quote(previous)}/{compose} up -d",
                approved=True,
            )
            evidence.append(restart.model_dump(mode="json"))
        return evidence

    async def verify(self, target_name: str) -> dict[str, Any]:
        target = self.target(target_name)
        runtime = self.runtime(target)
        await runtime.prepare()
        try:
            current = PurePosixPath(target.deployment_path) / "current"
            healthy, evidence = await self._verify_runtime(runtime, target, current)
            return {"healthy": healthy, "evidence": evidence}
        finally:
            await runtime.cleanup()

    async def rollback(self, target_name: str, *, approved: bool) -> dict[str, Any]:
        if not approved:
            raise PolicyDenied("Rollback requires explicit approval")
        target = self.target(target_name)
        with self.database.session() as session:
            last = session.scalar(
                select(DeploymentRun)
                .where(
                    DeploymentRun.target_name == target_name,
                    DeploymentRun.status == "healthy",
                )
                .order_by(DeploymentRun.created_at.desc())
            )
            if last is None or not last.previous_release:
                raise DeploymentError("No previous healthy release recorded")
            previous = last.previous_release
        runtime = self.runtime(target)
        await runtime.prepare()
        try:
            evidence = await self._rollback_runtime(
                runtime,
                target,
                previous,
                PurePosixPath(target.deployment_path) / "current",
            )
            verified = await self.verify(target_name)
            return {"restored": previous, "commands": evidence, "verification": verified}
        finally:
            await runtime.cleanup()

    def status(self, target_name: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            runs = session.scalars(
                select(DeploymentRun)
                .where(DeploymentRun.target_name == target_name)
                .order_by(DeploymentRun.created_at.desc())
            ).all()
            return [
                {
                    "id": run.id,
                    "release": run.release_id,
                    "status": run.status,
                    "previous": run.previous_release,
                    "created_at": run.created_at.isoformat(),
                }
                for run in runs
            ]

    async def logs(self, target_name: str, *, lines: int = 200) -> dict[str, Any]:
        target = self.target(target_name)
        runtime = self.runtime(target)
        await runtime.prepare()
        try:
            current = PurePosixPath(target.deployment_path) / "current"
            compose = shlex.quote(target.compose_file)
            result = await runtime.execute(
                f"docker compose -f {shlex.quote(str(current))}/{compose} "
                f"logs --no-color --tail {max(1, min(lines, 5000))}",
                approved=True,
            )
            return result.model_dump(mode="json")
        finally:
            await runtime.cleanup()

    def _save_run(self, run: DeploymentRun) -> None:
        with self.database.session() as session:
            stored = session.get(DeploymentRun, run.id)
            if stored is None:
                session.add(run)
                return
            stored.status = run.status
            stored.evidence = run.evidence
            stored.previous_release = run.previous_release
