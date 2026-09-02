"""Persistence for workspace runs: the executing half of a plan.

A run is the row that survives the process. Everything a run produces is an
ordinary artifact in the workspace folder and every step it works is an ordinary
task, so this table holds only what neither can express — which goal is being
executed, where the executor stopped, and the timeline a reader needs when they
come back tomorrow and the event bus has long since forgotten.

Kept apart from :mod:`daino.workbench.service` because that module is about
files and this one is about execution state; they share a database and nothing
else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from daino.persistence import Database
from daino.persistence.models import WorkspaceRun as RunRow
from daino.persistence.models import WorkspaceRunStep as StepRow
from daino.persistence.models import WorkspaceTask as TaskRow
from daino.utils.ids import new_id
from daino.workbench.models import (
    PendingApproval,
    RunStatus,
    RunStep,
    RunStepKind,
    WorkspaceRun,
)

#: States in which the executor is still attached to the run. A run in any other
#: state is history: it can be read, but nothing will move it again.
ACTIVE_STATES: frozenset[str] = frozenset(
    {"pending", "running", "paused", "waiting_for_approval", "waiting_for_user"}
)

#: How many timeline lines a run carries in its API payload. A long run can
#: produce hundreds; the panel shows the recent end of it.
MAX_STEPS = 400


class RunStore:
    """Create, read, and advance workspace runs."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        workspace_id: str,
        *,
        goal: str = "",
        skill: str = "",
        profile: str = "",
    ) -> WorkspaceRun:
        identifier = new_id("wsrun")
        with self.database.session() as session:
            session.add(
                RunRow(
                    id=identifier,
                    workspace_id=workspace_id,
                    goal=goal.strip(),
                    status="pending",
                    skill=skill,
                    profile=profile,
                )
            )
        return self.get(identifier)

    def get(self, run_id: str) -> WorkspaceRun | None:
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return None
            return self._describe(session, row)

    def active_for(self, workspace_id: str) -> WorkspaceRun | None:
        """The run the executor is still attached to, if there is one."""
        with self.database.session() as session:
            row = session.scalars(
                select(RunRow)
                .where(RunRow.workspace_id == workspace_id)
                .where(RunRow.status.in_(tuple(ACTIVE_STATES)))
                .order_by(RunRow.created_at.desc())
            ).first()
            return None if row is None else self._describe(session, row)

    def latest_for(self, workspace_id: str) -> WorkspaceRun | None:
        """The active run, or the most recent finished one to report on."""
        with self.database.session() as session:
            row = session.scalars(
                select(RunRow)
                .where(RunRow.workspace_id == workspace_id)
                .order_by(RunRow.created_at.desc())
            ).first()
            return None if row is None else self._describe(session, row)

    def history_for(self, workspace_id: str, *, limit: int = 20) -> list[WorkspaceRun]:
        with self.database.session() as session:
            rows = session.scalars(
                select(RunRow)
                .where(RunRow.workspace_id == workspace_id)
                .order_by(RunRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._describe(session, row, with_steps=False) for row in rows]

    def update(self, run_id: str, **fields: Any) -> WorkspaceRun | None:
        """Set run fields, stamping ``finished_at`` when it reaches an end state."""
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return None
            metadata = fields.pop("metadata", None)
            status = fields.get("status")
            for name, value in fields.items():
                setattr(row, name, value)
            if metadata is not None:
                # Replaced wholesale rather than merged: the caller reads the
                # run first, so a partial write would silently drop keys.
                row.metadata_json = dict(metadata)
            if status == "running" and row.started_at is None:
                row.started_at = datetime.now(UTC)
            if status is not None and status not in ACTIVE_STATES:
                row.finished_at = datetime.now(UTC)
            return self._describe(session, row)

    def add_step(
        self,
        run_id: str,
        kind: RunStepKind,
        message: str,
        *,
        task_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> RunStep:
        identifier = new_id("wsstep")
        with self.database.session() as session:
            session.add(
                StepRow(
                    id=identifier,
                    run_id=run_id,
                    task_id=task_id,
                    kind=kind,
                    message=message.strip(),
                    detail=dict(detail or {}),
                )
            )
        return RunStep(
            id=identifier,
            kind=kind,
            task_id=task_id,
            message=message.strip(),
            detail=dict(detail or {}),
        )

    def reconcile(self) -> list[str]:
        """Recover runs the process died holding.

        A run marked ``running`` with no executor behind it is a lie the GUI
        would render as live work. On startup those become ``paused``: the plan
        and every finished task are intact, so the honest offer is Resume.
        """
        recovered: list[str] = []
        with self.database.session() as session:
            rows = session.scalars(
                select(RunRow).where(RunRow.status.in_(("running", "pending")))
            ).all()
            for row in rows:
                row.status = "paused"
                row.error = row.error or "Interrupted when Daino stopped. Resume to continue."
                recovered.append(row.id)
        return recovered

    # ---------------------------------------------------------------- reading

    def _describe(
        self, session: Any, row: RunRow, *, with_steps: bool = True
    ) -> WorkspaceRun:
        tasks = session.scalars(
            select(TaskRow).where(TaskRow.workspace_id == row.workspace_id)
        ).all()
        steps: list[RunStep] = []
        if with_steps:
            found = session.scalars(
                select(StepRow)
                .where(StepRow.run_id == row.id)
                .order_by(StepRow.created_at.desc(), StepRow.id.desc())
                .limit(MAX_STEPS)
            ).all()
            steps = [
                RunStep(
                    id=item.id,
                    kind=_step_kind(item.kind),
                    task_id=item.task_id,
                    message=item.message,
                    detail=dict(item.detail or {}),
                    created_at=item.created_at,
                )
                for item in reversed(found)
            ]
        metadata = dict(row.metadata_json or {})
        approval = metadata.get("pending_approval")
        return WorkspaceRun(
            id=row.id,
            workspace_id=row.workspace_id,
            goal=row.goal,
            status=_run_status(row.status),
            current_task_id=row.current_task_id,
            error=row.error,
            skill=row.skill,
            profile=row.profile,
            started_at=row.started_at,
            finished_at=row.finished_at,
            total_tasks=len(tasks),
            completed_tasks=sum(item.status == "completed" for item in tasks),
            pending_approval=(
                PendingApproval.model_validate(approval) if isinstance(approval, dict) else None
            ),
            steps=steps,
            metadata=metadata,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


#: Every status a run row may legitimately hold.
_KNOWN_STATES = ACTIVE_STATES | {"completed", "failed", "cancelled"}


def _run_status(value: str) -> RunStatus:
    """Narrow a stored status, defaulting rather than raising on a stale value."""
    return value if value in _KNOWN_STATES else "failed"  # type: ignore[return-value]


def _step_kind(value: str) -> RunStepKind:
    known = {
        "run_started",
        "run_finished",
        "task_started",
        "task_completed",
        "task_failed",
        "task_skipped",
        "artifact",
        "source",
        "note",
        "steer",
        "approval",
    }
    return value if value in known else "note"  # type: ignore[return-value]
