"""Observable deployment operations."""

from __future__ import annotations

from typing import Any

from vasuki.application.context import ProjectContext
from vasuki.deployment import DeploymentManager
from vasuki.events import (
    DeploymentFailed,
    DeploymentProgress,
    DeploymentStarted,
    DeploymentVerified,
    RollbackCompleted,
    RollbackStarted,
)


class DeploymentApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.manager = DeploymentManager(context.root, context.settings, context.database)

    def targets(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "type": target.type,
                "host": target.host or "localhost",
                "environment": target.environment,
                "runtime": "Remote SSH" if target.type == "ssh" else "Local Docker",
                "approval": (
                    "required" if target.environment.lower() in {"production", "prod"} else "policy"
                ),
            }
            for name, target in self.context.settings.deployment.targets.items()
        ]

    async def inspect(self, target: str) -> dict[str, Any]:
        self.context.events.publish(DeploymentStarted(target=target, action="inspect"))
        try:
            result = await self.manager.inspect(target)
            self.context.events.publish(
                DeploymentProgress(target=target, stage="Inspection complete", progress=1)
            )
            return result
        except Exception as exc:
            self.context.events.publish(DeploymentFailed(target=target, error=str(exc)))
            raise

    async def plan(self, target: str) -> object:
        self.context.events.publish(DeploymentStarted(target=target, action="plan"))
        return await self.manager.create_plan(target)

    async def apply(
        self,
        target: str,
        *,
        approved: bool,
        mission_id: str | None = None,
    ) -> object:
        self.context.events.publish(DeploymentStarted(target=target, action="apply"))
        try:
            result = await self.manager.apply(
                target,
                approved=approved,
                mission_id=mission_id,
            )
            self.context.events.publish(
                DeploymentVerified(
                    mission_id=mission_id,
                    target=target,
                    healthy=result.status == "healthy",
                )
            )
            return result
        except Exception as exc:
            self.context.events.publish(
                DeploymentFailed(mission_id=mission_id, target=target, error=str(exc))
            )
            raise

    async def rollback(self, target: str, *, approved: bool) -> dict[str, Any]:
        self.context.events.publish(RollbackStarted(target=target))
        result = await self.manager.rollback(target, approved=approved)
        self.context.events.publish(RollbackCompleted(target=target, result=result))
        return result

    async def verify(self, target: str) -> dict[str, Any]:
        result = await self.manager.verify(target)
        self.context.events.publish(
            DeploymentVerified(target=target, healthy=bool(result.get("healthy")))
        )
        return result
