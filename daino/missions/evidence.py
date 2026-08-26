"""Tamper-evident mission evidence exports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from daino.config import paths
from daino.git import GitClient
from daino.persistence import Database
from daino.persistence.models import (
    Artifact,
    Mission,
    ModelCall,
    RequirementVersion,
    Review,
    Task,
    ToolCall,
    VerificationRun,
)
from daino.utils.ids import new_id


class EvidenceExporter:
    def __init__(self, root: Path, database: Database) -> None:
        self.root = root.resolve()
        self.database = database

    def collect(self, mission_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"Unknown mission {mission_id}")
            requirement = session.scalar(
                select(RequirementVersion)
                .where(RequirementVersion.mission_id == mission_id)
                .order_by(RequirementVersion.version.desc())
            )
            tasks = session.scalars(
                select(Task).where(Task.mission_id == mission_id).order_by(Task.created_at)
            ).all()
            model_calls = session.scalars(
                select(ModelCall).where(ModelCall.mission_id == mission_id)
            ).all()
            tool_calls = session.scalars(
                select(ToolCall).where(ToolCall.mission_id == mission_id)
            ).all()
            verifications = session.scalars(
                select(VerificationRun).where(VerificationRun.mission_id == mission_id)
            ).all()
            reviews = session.scalars(select(Review).where(Review.mission_id == mission_id)).all()
            diff = ""
            files_changed: list[str] = []
            if mission.workspace_path and Path(mission.workspace_path).exists():
                git = GitClient(Path(mission.workspace_path))
                if mission.initial_revision:
                    diff = git.diff(mission.initial_revision)
                    files_changed = git.run(
                        "diff", "--name-only", mission.initial_revision
                    ).stdout.splitlines()
            return {
                "mission": {
                    "id": mission.id,
                    "request": mission.request,
                    "mode": mission.mode,
                    "status": mission.status,
                    "workspace": mission.workspace_path,
                    "branch": mission.branch,
                    "initial_revision": mission.initial_revision,
                    "commit_hash": mission.final_revision,
                    "rollback_point": mission.initial_revision,
                    "failure": mission.failure,
                },
                "approved_requirements": requirement.content if requirement else None,
                "tasks": [
                    {
                        **task.specification,
                        "status": task.status,
                        "attempt_count": task.attempt_count,
                        "evidence": task.evidence,
                    }
                    for task in tasks
                ],
                "files_changed": files_changed,
                "git_diff": diff,
                "model_calls": [
                    {
                        "role": call.role,
                        "provider": call.provider,
                        "model": call.model,
                        "selection_reason": call.selection_reason,
                        "included_files": call.included_files,
                        "input_tokens": call.input_tokens,
                        "output_tokens": call.output_tokens,
                        "latency_ms": call.latency_ms,
                        "success": call.success,
                    }
                    for call in model_calls
                ],
                "tool_calls": [
                    {
                        "tool": call.tool,
                        "arguments": call.arguments,
                        "result": call.result_summary,
                        "success": call.success,
                    }
                    for call in tool_calls
                ],
                "verification": [run.report for run in verifications],
                "reviews": [review.report for review in reviews],
                "known_limitations": [],
            }

    def export(self, mission_id: str, format: str = "markdown") -> Path:
        evidence = self.collect(mission_id)
        directory = paths.state_dir(self.root) / "artifacts" / mission_id
        directory.mkdir(parents=True, exist_ok=True)
        if format == "json":
            path = directory / "evidence.json"
            content = json.dumps(evidence, indent=2, default=str)
        elif format == "markdown":
            path = directory / "evidence.md"
            mission = evidence["mission"]
            content = (
                f"# Daino evidence: {mission_id}\n\n"
                f"- Status: {mission['status']}\n"
                f"- Mode: {mission['mode']}\n"
                f"- Branch: `{mission['branch']}`\n"
                f"- Commit: `{mission['commit_hash']}`\n"
                f"- Rollback point: `{mission['rollback_point']}`\n\n"
                f"## Original request\n\n{mission['request']}\n\n"
                f"## Requirements\n\n```json\n"
                f"{json.dumps(evidence['approved_requirements'], indent=2)}\n```\n\n"
                f"## Tasks\n\n```json\n{json.dumps(evidence['tasks'], indent=2)}\n```\n\n"
                f"## Files changed\n\n"
                + "\n".join(f"- `{path}`" for path in evidence["files_changed"])
                + "\n\n## Verification\n\n```json\n"
                + json.dumps(evidence["verification"], indent=2)
                + "\n```\n\n## Independent reviews\n\n```json\n"
                + json.dumps(evidence["reviews"], indent=2)
                + "\n```\n\n## Git diff\n\n```diff\n"
                + evidence["git_diff"]
                + "\n```\n"
            )
        else:
            raise ValueError("Format must be markdown or json")
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.database.session() as session:
            session.add(
                Artifact(
                    id=new_id("artifact"),
                    mission_id=mission_id,
                    type=f"evidence-{format}",
                    path=str(path),
                    digest=digest,
                    metadata_json={"sha256": digest},
                )
            )
        return path
