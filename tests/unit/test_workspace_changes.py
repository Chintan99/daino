"""Change sets: one act's worth of edits, reviewable and undoable together.

The point of these is that they add an index and take nothing away — every
assertion here about rejecting a change is really an assertion about the
existing ``.history`` mechanism still being the thing that does the work.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.config import default_settings, save_settings
from daino.events import EventBus
from daino.persistence import Database
from daino.workbench.changes import ChangeSetStore
from daino.workbench.service import WorkbenchError, WorkbenchService


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[WorkbenchService]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield WorkbenchService(tmp_path, database, events=EventBus())
    database.engine.dispose()


@pytest.fixture
def changes(workbench: WorkbenchService) -> ChangeSetStore:
    return ChangeSetStore(workbench.database, workbench)


def test_everything_one_step_touched_is_grouped(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "proposal.md", "First draft", author="user")

    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "proposal.md", "Second draft", author="agent")
    workbench.write_artifact(workspace.id, "research.md", "Sources", author="agent")
    change = changes.record(workspace.id, before=before, task_id="t-1", summary="Drafted")

    assert change is not None
    assert {(item.path, item.action) for item in change.entries} == {
        ("proposal.md", "updated"),
        ("research.md", "created"),
    }
    assert change.status == "open"
    assert all(item.status == "pending" for item in change.entries)


def test_a_step_that_changed_nothing_files_nothing(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    """An investigation step produces findings, not files. That is not a change."""
    workspace = workbench.create("Research")
    before = changes.snapshot(workspace.id)

    assert changes.record(workspace.id, before=before, summary="Read three pages") is None


def test_a_rejected_change_restores_the_previous_revision(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "proposal.md", "The good draft\n", author="user")

    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "proposal.md", "The agent's rewrite\n", author="agent")
    change = changes.record(workspace.id, before=before)
    assert change is not None

    decided = changes.decide(change.id, "proposal.md", accepted=False)

    assert decided.status == "rejected"
    assert workbench.artifact(workspace.id, "proposal.md").content == "The good draft\n"
    # And the rejection is itself a revision, so it too can be undone.
    versions = [item.version for item in workbench.revisions(workspace.id, "proposal.md")]
    assert len(versions) == 3


def test_rejecting_a_created_artifact_removes_it(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "draft.md", "Unwanted", author="agent")

    change = changes.record(workspace.id, before=before)
    assert change is not None
    changes.decide(change.id, "draft.md", accepted=False)

    with pytest.raises(WorkbenchError):
        workbench.artifact(workspace.id, "draft.md")
    # The history survives the removal — nothing here destroys a version.
    assert workbench.revisions(workspace.id, "draft.md")


def test_accepting_leaves_the_artifact_exactly_as_it_is(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "proposal.md", "Old", author="user")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "proposal.md", "New", author="agent")
    change = changes.record(workspace.id, before=before)
    assert change is not None

    decided = changes.decide(change.id, "proposal.md", accepted=True)

    assert decided.status == "accepted"
    assert workbench.artifact(workspace.id, "proposal.md").content == "New"


def test_a_set_decided_both_ways_is_partial(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    """Per-artifact decisions are the point; the set reports what happened."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "a.md", "old a", author="user")
    workbench.write_artifact(workspace.id, "b.md", "old b", author="user")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "a.md", "new a", author="agent")
    workbench.write_artifact(workspace.id, "b.md", "new b", author="agent")
    change = changes.record(workspace.id, before=before)
    assert change is not None

    changes.decide(change.id, "a.md", accepted=True)
    decided = changes.decide(change.id, "b.md", accepted=False)

    assert decided.status == "partial"
    assert workbench.artifact(workspace.id, "a.md").content == "new a"
    assert workbench.artifact(workspace.id, "b.md").content == "old b"


def test_accept_all_decides_every_pending_artifact(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "a.md", "a", author="agent")
    workbench.write_artifact(workspace.id, "b.md", "b", author="agent")
    change = changes.record(workspace.id, before=before)
    assert change is not None

    decided = changes.decide_all(change.id, accepted=True)

    assert decided.status == "accepted"


def test_the_diff_shows_this_change_not_the_current_state(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    """A later edit must not rewrite the history of an earlier review."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "proposal.md", "one\ntwo\n", author="user")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "proposal.md", "one\nthree\n", author="agent")
    change = changes.record(workspace.id, before=before)
    assert change is not None
    # Something else edits the file afterwards.
    workbench.write_artifact(workspace.id, "proposal.md", "totally different\n", author="user")

    diff = changes.diff(change.id, "proposal.md")

    markers = {(line.marker, line.text) for line in diff.lines}
    assert ("-", "two") in markers
    assert ("+", "three") in markers
    assert not any(text == "totally different" for _, text in markers)


def test_changes_can_be_listed_for_one_run(
    workbench: WorkbenchService, changes: ChangeSetStore
) -> None:
    workspace = workbench.create("Proposal")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "a.md", "a", author="agent")
    changes.record(workspace.id, before=before, run_id="run-1")
    before = changes.snapshot(workspace.id)
    workbench.write_artifact(workspace.id, "b.md", "b", author="agent")
    changes.record(workspace.id, before=before, run_id="run-2")

    assert len(changes.list_for(workspace.id)) == 2
    assert len(changes.list_for(workspace.id, run_id="run-1")) == 1
