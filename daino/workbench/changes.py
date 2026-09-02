"""Change sets: everything one step of the work touched, reviewed together.

The per-file history in ``.history`` is unchanged and remains the source of
truth — every version is still there, still restorable one file at a time. What
it cannot express is that seven of those versions were *one act*: a task that
rewrote the proposal, extended the comparison, and added four sources. Reviewing
that file by file means reconstructing the act from its debris.

So a change set is an index, never a store. It records which artifact moved from
which revision to which, and every operation on it is expressed in terms of the
history that already exists: rejecting one artifact restores the revision it had
before, using the same code path the History panel has always used. Delete the
change-set rows and nothing is lost but the grouping.

The comparison is computed from the history index rather than from the agent's
reported diffs, because the question is "what does this workspace hold now that
it did not before", and a file the agent edited twice, or edited and then
reverted, has to answer that correctly.
"""

from __future__ import annotations

import contextlib

from sqlalchemy import select

from daino.persistence import Database
from daino.persistence.models import WorkspaceChangeEntry as EntryRow
from daino.persistence.models import WorkspaceChangeSet as SetRow
from daino.schemas import FileDiff
from daino.tools.diffing import build_file_diff
from daino.utils.ids import new_id
from daino.workbench.models import ChangeEntry, ChangeSet
from daino.workbench.service import WorkbenchError, WorkbenchService


class ChangeSetStore:
    """Group, review, and undo the artifacts one operation changed."""

    def __init__(self, database: Database, workbench: WorkbenchService) -> None:
        self.database = database
        self.workbench = workbench

    # --------------------------------------------------------------- writing

    def snapshot(self, workspace_id: str) -> dict[str, int]:
        """Every artifact's newest revision number, right now.

        Version 0 means "no history yet", which covers both a file that does not
        exist and one that predates the workspace — either way, rejecting a
        change to it means removing what the run added.
        """
        workspace = self.workbench.get(workspace_id)
        found: dict[str, int] = {}
        for artifact in workspace.artifacts:
            revisions = self.workbench.revisions(workspace_id, artifact.path)
            found[artifact.path] = revisions[0].version if revisions else 0
        return found

    def record(
        self,
        workspace_id: str,
        *,
        before: dict[str, int],
        run_id: str = "",
        task_id: str = "",
        summary: str = "",
    ) -> ChangeSet | None:
        """Compare against a snapshot and file what moved. None when nothing did."""
        after = self.snapshot(workspace_id)
        entries: list[tuple[str, str, int, int]] = []
        for path, version in sorted(after.items()):
            previous = before.get(path, 0)
            if version == previous:
                continue
            action = "created" if path not in before else "updated"
            entries.append((path, action, previous, version))
        for path, version in sorted(before.items()):
            if path not in after:
                entries.append((path, "deleted", version, 0))
        if not entries:
            return None

        identifier = new_id("wscs")
        with self.database.session() as session:
            session.add(
                SetRow(
                    id=identifier,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    summary=summary.strip()[:2_000],
                )
            )
            for path, action, previous, version in entries:
                session.add(
                    EntryRow(
                        id=new_id("wsce"),
                        change_set_id=identifier,
                        path=path,
                        action=action,
                        before_version=previous,
                        after_version=version,
                    )
                )
        # A change set is an index into history, so the history it indexes has
        # to outlive the retention cap. Without this, a workspace busy enough to
        # roll past MAX_REVISIONS leaves its older change sets undiffable and
        # unrejectable — the exact recovery the panel offers.
        for path, _action, previous, version in entries:
            self.workbench.pin_revisions(workspace_id, path, (previous, version))
        return self.get(identifier)

    # --------------------------------------------------------------- reading

    def get(self, change_set_id: str) -> ChangeSet:
        with self.database.session() as session:
            row = session.get(SetRow, change_set_id)
            if row is None:
                raise WorkbenchError(f"Unknown change set {change_set_id}")
            entries = session.scalars(
                select(EntryRow)
                .where(EntryRow.change_set_id == change_set_id)
                .order_by(EntryRow.path)
            ).all()
            return _describe(row, entries)

    def list_for(
        self, workspace_id: str, *, run_id: str = "", limit: int = 50
    ) -> list[ChangeSet]:
        with self.database.session() as session:
            query = select(SetRow).where(SetRow.workspace_id == workspace_id)
            if run_id:
                query = query.where(SetRow.run_id == run_id)
            rows = session.scalars(query.order_by(SetRow.created_at.desc()).limit(limit)).all()
            identifiers = [row.id for row in rows]
        return [self.get(identifier) for identifier in identifiers]

    def diff(self, change_set_id: str, path: str) -> FileDiff:
        """What this change did to one artifact, line by line.

        Both sides come from the history the workspace already keeps, so a diff
        stays readable long after the run — and stays honest if the file has been
        edited again since, because it shows *this* change rather than the gap
        between then and now.
        """
        change = self.get(change_set_id)
        entry = next((item for item in change.entries if item.path == path), None)
        if entry is None:
            raise WorkbenchError(f"{path} is not part of this change")
        return build_file_diff(
            path,
            self._text(change.workspace_id, path, entry.before_version),
            self._text(change.workspace_id, path, entry.after_version),
        )

    # -------------------------------------------------------------- deciding

    def decide(self, change_set_id: str, path: str, *, accepted: bool) -> ChangeSet:
        """Keep or undo one artifact's change.

        Rejecting restores the revision the artifact had before this change,
        which itself becomes a new revision — so a rejection is as undoable as
        the change it undid. Nothing is deleted from history either way.
        """
        change = self.get(change_set_id)
        entry = next((item for item in change.entries if item.path == path), None)
        if entry is None:
            raise WorkbenchError(f"{path} is not part of this change")
        if not accepted and entry.status != "rejected":
            self._revert(change.workspace_id, entry)
        with self.database.session() as session:
            row = session.get(EntryRow, entry.id)
            if row is not None:
                row.status = "accepted" if accepted else "rejected"
        return self._settle(change_set_id)

    def decide_all(self, change_set_id: str, *, accepted: bool) -> ChangeSet:
        change = self.get(change_set_id)
        for entry in change.entries:
            if entry.status == "pending":
                self.decide(change_set_id, entry.path, accepted=accepted)
        return self.get(change_set_id)

    # ---------------------------------------------------------------- pieces

    def _revert(self, workspace_id: str, entry: ChangeEntry) -> None:
        if entry.before_version == 0:
            # The change created this artifact, so undoing it means removing it.
            # The history stays, which is what makes even this reversible.
            with contextlib.suppress(WorkbenchError):
                self.workbench.delete_artifact(workspace_id, entry.path)
            return
        self.workbench.restore_revision(workspace_id, entry.path, entry.before_version)

    def _settle(self, change_set_id: str) -> ChangeSet:
        """Roll the per-artifact decisions up into the set's own status."""
        change = self.get(change_set_id)
        statuses = {entry.status for entry in change.entries}
        status = (
            "open"
            if "pending" in statuses
            else "accepted"
            if statuses == {"accepted"}
            else "rejected"
            if statuses == {"rejected"}
            else "partial"
        )
        with self.database.session() as session:
            row = session.get(SetRow, change_set_id)
            if row is not None:
                row.status = status
        return self.get(change_set_id)

    def _text(self, workspace_id: str, path: str, version: int) -> str | None:
        if version == 0:
            return None
        try:
            return self.workbench.revision_content(workspace_id, path, version)
        except WorkbenchError:
            return None


def _describe(row: SetRow, entries: list[EntryRow]) -> ChangeSet:
    return ChangeSet(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        task_id=row.task_id,
        summary=row.summary,
        status=row.status,  # type: ignore[arg-type]
        entries=[
            ChangeEntry(
                id=item.id,
                path=item.path,
                action=item.action,  # type: ignore[arg-type]
                before_version=item.before_version,
                after_version=item.after_version,
                status=item.status,  # type: ignore[arg-type]
                summary=item.summary,
            )
            for item in entries
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
