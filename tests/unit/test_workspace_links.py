"""Provenance and staleness: which document came from what, and what moved.

The mechanism is one number — the target's revision when the edge was made — so
these tests are mostly about that number meaning what it should after the sorts
of edits a workspace actually sees.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.config import default_settings, save_settings
from daino.events import EventBus
from daino.persistence import Database
from daino.workbench.links import LinkStore
from daino.workbench.service import WorkbenchService


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[WorkbenchService]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield WorkbenchService(tmp_path, database, events=EventBus())
    database.engine.dispose()


@pytest.fixture
def links(workbench: WorkbenchService) -> LinkStore:
    return LinkStore(workbench.database, workbench)


def test_a_document_written_from_another_records_where_it_came_from(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "requirements.md", "reqs", author="user")
    workbench.write_artifact(workspace.id, "proposal.md", "draft", author="agent")

    link = links.link(
        workspace.id,
        source_path="proposal.md",
        target_path="requirements.md",
        relation="derived_from",
    )

    assert link.source_path == "proposal.md"
    assert link.target_path == "requirements.md"
    assert link.target_revision == 1
    assert links.stale(workspace.id) == []


def test_a_document_falls_behind_when_its_source_changes(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    """The whole point: the proposal no longer describes the architecture."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "architecture.md", "v1", author="agent")
    workbench.write_artifact(workspace.id, "proposal.md", "written from v1", author="agent")
    links.link(
        workspace.id,
        source_path="proposal.md",
        target_path="architecture.md",
        relation="derived_from",
    )

    workbench.write_artifact(workspace.id, "architecture.md", "v2 — different", author="user")

    stale = links.stale(workspace.id)
    assert [item.path for item in stale] == ["proposal.md"]
    assert stale[0].source_of_truth == "architecture.md"
    assert "has changed" in stale[0].reason


def test_the_derived_document_changing_does_not_make_it_stale(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    """Staleness is about the source moving, not about the document being edited."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "architecture.md", "v1", author="agent")
    workbench.write_artifact(workspace.id, "proposal.md", "draft", author="agent")
    links.link(
        workspace.id,
        source_path="proposal.md",
        target_path="architecture.md",
        relation="derived_from",
    )

    workbench.write_artifact(workspace.id, "proposal.md", "second draft", author="user")

    assert links.stale(workspace.id) == []


def test_a_warning_can_be_dismissed_and_stays_dismissed(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "architecture.md", "v1", author="agent")
    workbench.write_artifact(workspace.id, "proposal.md", "draft", author="agent")
    link = links.link(
        workspace.id,
        source_path="proposal.md",
        target_path="architecture.md",
        relation="derived_from",
    )
    workbench.write_artifact(workspace.id, "architecture.md", "v2", author="user")
    assert links.stale(workspace.id)

    links.acknowledge(workspace.id, link.id)

    assert links.stale(workspace.id) == []
    # And it warns again the next time the source actually moves.
    workbench.write_artifact(workspace.id, "architecture.md", "v3", author="user")
    assert [item.path for item in links.stale(workspace.id)] == ["proposal.md"]


def test_a_citation_is_not_a_dependency(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    """``references`` means "I mentioned it", which nothing should chase."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "notes.md", "v1", author="user")
    workbench.write_artifact(workspace.id, "proposal.md", "draft", author="agent")
    links.link(
        workspace.id,
        source_path="proposal.md",
        target_path="notes.md",
        relation="references",
    )

    workbench.write_artifact(workspace.id, "notes.md", "v2", author="user")

    assert links.stale(workspace.id) == []


def test_linking_the_same_pair_twice_updates_rather_than_duplicates(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "a.md", "v1", author="user")
    workbench.write_artifact(workspace.id, "b.md", "draft", author="agent")
    links.link(workspace.id, source_path="b.md", target_path="a.md", relation="derived_from")
    workbench.write_artifact(workspace.id, "a.md", "v2", author="user")

    links.link(workspace.id, source_path="b.md", target_path="a.md", relation="derived_from")

    assert len(links.links_for(workspace.id)) == 1
    # Re-linking after regenerating the document clears the warning, because
    # the document has just been written against the newer version.
    assert links.stale(workspace.id) == []


def test_work_in_another_tab_is_linked_without_being_owned(
    workbench: WorkbenchService, links: LinkStore
) -> None:
    """A diagram lives in DESIGN; the workspace records that it caused it."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "requirements.md", "reqs", author="user")

    links.link(
        workspace.id,
        source_path="dsn_123",
        source_kind="design",
        target_path="requirements.md",
        relation="describes",
        title="Architecture diagram",
    )

    linked = links.links_for(workspace.id)
    assert [(item.source_kind, item.title) for item in linked] == [
        ("design", "Architecture diagram")
    ]
    # And it goes stale with its source, like any other derived thing.
    workbench.write_artifact(workspace.id, "requirements.md", "changed", author="user")
    assert [item.path for item in links.stale(workspace.id)] == ["dsn_123"]
