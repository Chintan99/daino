"""Recoverable workspace checkpoint operations."""

from __future__ import annotations

import builtins
from pathlib import Path

from sqlalchemy import select

from daino.application.context import ProjectContext
from daino.events import CheckpointCreated
from daino.git import GitClient
from daino.persistence.models import Checkpoint, Mission
from daino.workspace import Workspace, WorkspaceManager


class CheckpointApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.manager = WorkspaceManager(context.root)

    def list(self, mission_id: str | None = None) -> list[Checkpoint]:
        with self.context.database.session() as session:
            query = select(Checkpoint).order_by(Checkpoint.created_at.desc())
            if mission_id:
                query = query.where(Checkpoint.mission_id == mission_id)
            items = session.scalars(query).all()
            for item in items:
                session.expunge(item)
            return list(items)

    def create(
        self,
        description: str = "Manual checkpoint",
        *,
        mission_id: str | None = None,
    ) -> Checkpoint:
        root = self.context.root
        branch = ""
        revision = ""
        status = ""
        if mission_id:
            with self.context.database.session() as session:
                mission = session.get(Mission, mission_id)
                if mission is None:
                    raise ValueError(f"Unknown mission {mission_id}")
                if mission.workspace_path:
                    root = Path(mission.workspace_path)
                branch = mission.branch or ""
                revision = mission.initial_revision or ""
        git = GitClient(root)
        if not revision:
            revision = git.revision()
        if not branch:
            branch = git.current_branch()
        status = git.status()
        workspace = Workspace(mission_id or "manual", root, branch, revision, status)
        checkpoint_id, archive = self.manager.checkpoint(
            workspace,
            description,
            mission_id=mission_id,
        )
        item = Checkpoint(
            id=checkpoint_id,
            mission_id=mission_id,
            revision=revision,
            archive_path=str(archive),
            description=description,
        )
        with self.context.database.session() as session:
            session.add(item)
        self.context.events.publish(
            CheckpointCreated(
                mission_id=mission_id,
                checkpoint_id=checkpoint_id,
                description=description,
            )
        )
        return item

    def restore(self, checkpoint_id: str) -> None:
        with self.context.database.session() as session:
            checkpoint = session.get(Checkpoint, checkpoint_id)
            if checkpoint is None or not checkpoint.archive_path:
                raise ValueError(f"Unknown or unrestorable checkpoint {checkpoint_id}")
            target = self.context.root
            if checkpoint.mission_id:
                mission = session.get(Mission, checkpoint.mission_id)
                if mission and mission.workspace_path:
                    target = Path(mission.workspace_path)
            archive = Path(checkpoint.archive_path)
        self.manager.restore_checkpoint(archive, target)

    def impact(self, checkpoint_id: str) -> dict[str, builtins.list[str]]:
        """Describe files in the archive before an approved restore."""
        import tarfile

        with self.context.database.session() as session:
            checkpoint = session.get(Checkpoint, checkpoint_id)
            if checkpoint is None or not checkpoint.archive_path:
                raise ValueError(f"Unknown or unrestorable checkpoint {checkpoint_id}")
            archive = Path(checkpoint.archive_path)
        with tarfile.open(archive, "r:gz") as handle:
            files = sorted(member.name for member in handle.getmembers() if member.isfile())
        return {"overwrite_or_create": files}
