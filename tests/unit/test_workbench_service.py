"""Workspaces: real folders in the project, with a plan and a boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.config import default_settings, save_settings
from daino.events import EventBus
from daino.persistence import Database
from daino.workbench.service import (
    HISTORY_DIR,
    MANIFEST,
    MAX_REVISIONS,
    UPLOADS_DIR,
    StaleArtifactError,
    WorkbenchError,
    WorkbenchService,
)


@pytest.fixture
def service(tmp_path: Path) -> Iterator[WorkbenchService]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield WorkbenchService(tmp_path, database, events=EventBus())
    database.engine.dispose()


def test_a_new_workspace_is_a_real_folder_in_the_project(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """The whole point of the in-project choice: ordinary files, ordinary tools."""
    workspace = service.create("Q3 pricing research", goal="Compare three vendors")

    folder = tmp_path / workspace.folder
    assert workspace.folder == ".daino/workspaces/q3-pricing-research"
    assert folder.is_dir()
    assert (folder / UPLOADS_DIR).is_dir()
    assert (folder / MANIFEST).is_file()
    assert workspace.goal == "Compare three vendors"


def test_the_folder_describes_itself_without_the_database(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """A workspace copied to another machine is still legible."""
    workspace = service.create("Onboarding", goal="Rewrite the guide", kind="prd")

    manifest = json.loads((tmp_path / workspace.folder / MANIFEST).read_text(encoding="utf-8"))

    assert manifest["name"] == "Onboarding"
    assert manifest["goal"] == "Rewrite the guide"
    assert manifest["kind"] == "prd"


def test_a_template_seeds_the_plan_and_the_documents(service: WorkbenchService) -> None:
    workspace = service.create("Pricing", kind="research")

    assert [task.content for task in workspace.tasks][0].startswith("Write the question")
    assert all(task.status == "pending" for task in workspace.tasks)
    findings = next(item for item in workspace.artifacts if item.path == "findings.md")
    assert findings.title == "Findings"
    content = service.artifact(workspace.id, "findings.md").content
    assert "## Question" in content and "## Sources" in content


def test_an_unknown_template_still_produces_a_usable_workspace(
    service: WorkbenchService,
) -> None:
    """Deleting a template must never strand the workspaces made from it."""
    workspace = service.create("Odd one", kind="no-such-template")

    assert workspace.kind == "general"
    assert workspace.tasks


def test_a_second_workspace_of_the_same_name_gets_its_own_folder(
    service: WorkbenchService,
) -> None:
    first = service.create("Pricing")
    second = service.create("Pricing")

    assert first.folder == ".daino/workspaces/pricing"
    assert second.folder == ".daino/workspaces/pricing-2"


def test_an_occupied_folder_is_never_adopted(service: WorkbenchService, tmp_path: Path) -> None:
    """Someone else's files must not silently become workspace artifacts."""
    existing = tmp_path / ".daino" / "workspaces" / "notes"
    existing.mkdir(parents=True)
    (existing / "private.md").write_text("not ours", encoding="utf-8")

    workspace = service.create("Notes")

    assert workspace.folder == ".daino/workspaces/notes-2"
    assert (existing / "private.md").read_text(encoding="utf-8") == "not ours"


def test_the_workspace_owns_its_own_subdirectories(service: WorkbenchService) -> None:
    """uploads/, .sources/ and .history/ are plumbing, not deliverables."""
    workspace = service.create("Pricing", kind="research")
    service.save_upload(workspace.id, "data.csv", b"a,b\n1,2\n")
    service.record_source(workspace.id, url="https://example.com", text="page text")
    service.write_artifact(workspace.id, "findings.md", "# Findings\n\nUpdated.\n")

    reloaded = service.get(workspace.id)
    paths = {item.path for item in reloaded.artifacts}

    assert paths == {"findings.md"}
    assert [item.path for item in reloaded.uploads] == [f"{UPLOADS_DIR}/data.csv"]
    with pytest.raises(WorkbenchError, match="reserved"):
        service.write_artifact(workspace.id, f"{UPLOADS_DIR}/sneaky.md", "no")


def test_a_path_cannot_escape_the_workspace(service: WorkbenchService) -> None:
    workspace = service.create("Pricing")

    for attempt in ("../../etc/passwd", "../other.md", "/etc/passwd"):
        with pytest.raises(WorkbenchError):
            service.artifact(workspace.id, attempt)


def test_an_artifact_title_comes_from_its_own_heading(service: WorkbenchService) -> None:
    workspace = service.create("Pricing")
    service.write_artifact(workspace.id, "notes.md", "# Vendor comparison\n\nBody.\n")

    artifact = next(item for item in service.get(workspace.id).artifacts if item.path == "notes.md")

    assert artifact.title == "Vendor comparison"
    assert artifact.preview == "Body."


def test_uploads_are_extracted_and_a_pipe_free_name_is_kept(
    service: WorkbenchService, tmp_path: Path
) -> None:
    workspace = service.create("Analysis", kind="analysis")

    upload = service.save_upload(workspace.id, "churn export.csv", b"region,churn\nEMEA,3\n")

    assert upload.path == f"{UPLOADS_DIR}/churn-export.csv"
    assert (tmp_path / workspace.folder / upload.path).is_file()
    assert upload.extracted_path.endswith("uploads/.extracted/churn-export.md")
    assert (tmp_path / upload.extracted_path).is_file()
    assert upload.warning == ""


def test_two_uploads_of_one_name_never_overwrite(service: WorkbenchService) -> None:
    workspace = service.create("Analysis")

    first = service.save_upload(workspace.id, "notes.txt", b"one")
    second = service.save_upload(workspace.id, "notes.txt", b"two")

    assert first.path != second.path
    assert service.artifact(workspace.id, first.path).content == "one"
    assert service.artifact(workspace.id, second.path).content == "two"


def test_an_unreadable_upload_is_stored_and_says_why(
    service: WorkbenchService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file Daino cannot parse is still the user's file."""
    from daino.workbench import extraction

    workspace = service.create("Analysis")

    def refuse(source: Path, *, force: bool = False) -> tuple[object, Path]:
        raise extraction.ExtractionError("needs the document parsers")

    monkeypatch.setattr(extraction, "extract_to_cache", refuse)

    upload = service.save_upload(workspace.id, "report.pdf", b"%PDF-1.4")

    assert upload.warning == "needs the document parsers"
    assert upload.extracted_path == ""
    assert service.get(workspace.id).uploads[0].path.endswith("report.pdf")


# ------------------------------------------------------------------- tasks


def test_the_plan_survives_being_re_emitted(service: WorkbenchService) -> None:
    """An agent restating the plan must not reset the progress already made."""
    workspace = service.create("Pricing")
    tasks = service.set_tasks(workspace.id, ["Read the docs", "Draft findings", "Review"])
    service.update_task(workspace.id, tasks[0].id, status="completed")

    after = service.set_tasks(workspace.id, ["Read the docs", "Draft findings", "Publish"])

    assert [item.content for item in after] == ["Read the docs", "Draft findings", "Publish"]
    assert after[0].status == "completed"
    assert after[2].status == "pending"


def test_a_completed_task_can_be_reopened(service: WorkbenchService) -> None:
    """Unlike a mission task, nothing here is terminal."""
    workspace = service.create("Pricing")
    task = service.add_task(workspace.id, "Draft findings")

    service.update_task(workspace.id, task.id, status="completed")
    reopened = service.update_task(workspace.id, task.id, status="in_progress")

    assert reopened.status == "in_progress"


def test_tasks_keep_an_explicit_order(service: WorkbenchService) -> None:
    workspace = service.create("Pricing")
    tasks = service.set_tasks(workspace.id, ["one", "two", "three"])

    reordered = service.reorder_tasks(workspace.id, [tasks[2].id, tasks[0].id])

    assert [item.content for item in reordered] == ["three", "one", "two"]


def test_two_tasks_may_share_their_text(service: WorkbenchService) -> None:
    """Session todos key on content and collide; these have real ids."""
    workspace = service.create("Pricing")

    first = service.add_task(workspace.id, "Review")
    second = service.add_task(workspace.id, "Review")

    assert first.id != second.id
    service.update_task(workspace.id, first.id, status="completed")
    statuses = {
        item.id: item.status for item in service.get(workspace.id).tasks if item.content == "Review"
    }
    assert statuses[first.id] == "completed"
    assert statuses[second.id] == "pending"


# ----------------------------------------------------------------- history


def test_overwriting_an_artifact_keeps_the_previous_text(
    service: WorkbenchService,
) -> None:
    """The case that matters: an agent rewriting something you had edited."""
    workspace = service.create("Pricing")
    service.write_artifact(workspace.id, "findings.md", "first draft", author="user")
    service.write_artifact(workspace.id, "findings.md", "agent rewrite", author="agent")

    revisions = service.revisions(workspace.id, "findings.md")

    assert [item.version for item in revisions] == [2, 1]
    assert revisions[0].author == "agent"
    assert service.revision_content(workspace.id, "findings.md", 1) == "first draft"


def test_an_unchanged_file_does_not_accumulate_revisions(
    service: WorkbenchService,
) -> None:
    """Several tools touching one file in a turn must not fill the history."""
    workspace = service.create("Pricing")
    service.write_artifact(workspace.id, "findings.md", "draft")
    for _ in range(3):
        service.record_revision(workspace.id, "findings.md", author="agent")

    assert len(service.revisions(workspace.id, "findings.md")) == 1


def test_a_revision_can_be_restored_without_losing_the_current_text(
    service: WorkbenchService,
) -> None:
    workspace = service.create("Pricing")
    service.write_artifact(workspace.id, "findings.md", "good version")
    service.write_artifact(workspace.id, "findings.md", "bad version")

    service.restore_revision(workspace.id, "findings.md", 1)

    assert service.artifact(workspace.id, "findings.md").content == "good version"
    # The mistake is still recoverable too.
    assert "bad version" in {
        service.revision_content(workspace.id, "findings.md", item.version)
        for item in service.revisions(workspace.id, "findings.md")
    }


def test_history_lives_beside_the_work_it_describes(
    service: WorkbenchService, tmp_path: Path
) -> None:
    workspace = service.create("Pricing")
    service.write_artifact(workspace.id, "findings.md", "one")
    service.write_artifact(workspace.id, "findings.md", "two")

    index = tmp_path / workspace.folder / HISTORY_DIR / "index.json"

    assert index.is_file()
    assert "findings.md" in json.loads(index.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- sources


def test_re_reading_a_page_updates_one_source_rather_than_adding_another(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """The Sources panel is a bibliography, not a request log."""
    workspace = service.create("Pricing", kind="research")

    service.record_source(workspace.id, url="https://example.com/a", title="A", text="body")
    service.record_source(workspace.id, url="https://example.com/a", title="A revised", text="body")
    service.record_source(workspace.id, url="https://example.com/b", title="B")

    sources = service.get(workspace.id).sources

    assert len(sources) == 2
    titles = {item.url: item.title for item in sources}
    assert titles["https://example.com/a"] == "A revised"
    cached = next(item for item in sources if item.url == "https://example.com/a")
    assert (tmp_path / cached.cache_path).read_text(encoding="utf-8").endswith("body\n")


# ----------------------------------------------------- listing and lifecycle


def test_listing_shows_progress_without_reading_any_document(
    service: WorkbenchService,
) -> None:
    workspace = service.create("Pricing", kind="research")
    tasks = service.get(workspace.id).tasks
    service.update_task(workspace.id, tasks[0].id, status="completed")

    summary = next(item for item in service.list_workspaces() if item.id == workspace.id)

    assert summary.done_count == 1
    assert summary.task_count == len(tasks)
    assert summary.artifact_count == 1


def test_archiving_hides_a_workspace_without_touching_its_files(
    service: WorkbenchService, tmp_path: Path
) -> None:
    workspace = service.create("Pricing")

    service.update(workspace.id, status="archived")

    assert workspace.id not in {item.id for item in service.list_workspaces()}
    assert workspace.id in {item.id for item in service.list_workspaces(include_archived=True)}
    assert (tmp_path / workspace.folder).is_dir()


def test_deleting_a_workspace_keeps_its_files_unless_asked(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """Removing a row is reversible; deleting written work is not."""
    kept = service.create("Kept")
    removed = service.create("Removed")

    service.delete(kept.id)
    service.delete(removed.id, remove_files=True)

    assert (tmp_path / kept.folder).is_dir()
    assert not (tmp_path / removed.folder).exists()
    # The shared parent survives while another workspace still needs it.
    assert (tmp_path / ".daino" / "workspaces").is_dir()
    with pytest.raises(WorkbenchError):
        service.get(kept.id)


def test_a_change_inside_a_workspace_folder_is_attributed_to_it(
    service: WorkbenchService,
) -> None:
    """What the history subscriber needs in order to know a file is ours."""
    workspace = service.create("Pricing")

    assert service.workspace_for_path(f"{workspace.folder}/findings.md") == workspace.id
    assert service.workspace_for_path("src/main.py") is None


def test_removing_the_last_workspace_leaves_the_project_as_it_was(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """The empty parent goes; the state directory around it stays."""
    workspace = service.create("Only one")

    service.delete(workspace.id, remove_files=True)

    assert not (tmp_path / ".daino" / "workspaces").exists()
    assert (tmp_path / ".daino").is_dir()


# ------------------------------------------- history from the file-change event


def test_an_edit_made_outside_the_workspace_api_still_gets_a_revision(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """The edits that most need a way back never come through this service.

    An agent's ``write`` and a manual save from the CODE tab both land as plain
    file writes; only the event tells us they happened.
    """
    from daino.events import FileChanged, GitChanged

    events = EventBus()
    service.events = events
    service.watch_file_changes(events)
    workspace = service.create("Pricing")
    document = tmp_path / workspace.folder / "findings.md"

    document.write_text("written by the agent", encoding="utf-8")
    events.publish(
        FileChanged(mission_id="m-1", path=f"{workspace.folder}/findings.md", action="write")
    )
    # A manual save from the CODE tab reports GitChanged, not FileChanged —
    # watching only one event would have captured only one of the two authors.
    document.write_text("corrected by hand", encoding="utf-8")
    events.publish(GitChanged(paths=[f"{workspace.folder}/findings.md"]))

    revisions = service.revisions(workspace.id, "findings.md")

    assert [(item.version, item.author) for item in revisions] == [(2, "user"), (1, "agent")]
    assert service.revision_content(workspace.id, "findings.md", 1) == "written by the agent"


def test_changes_outside_a_workspace_are_ignored(service: WorkbenchService, tmp_path: Path) -> None:
    from daino.events import FileChanged

    events = EventBus()
    service.watch_file_changes(events)
    workspace = service.create("Pricing")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1", encoding="utf-8")

    events.publish(FileChanged(path="src/main.py", action="write"))
    # The workspace's own plumbing is not a deliverable either.
    events.publish(FileChanged(path=f"{workspace.folder}/uploads/a.csv", action="write"))

    assert service.revisions(workspace.id, "src/main.py") == []
    assert service.revisions(workspace.id, "uploads/a.csv") == []


# ------------------------------------------ reachable from the ordinary tools


def test_workspace_documents_stay_searchable_inside_the_state_directory(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """Living under ``.daino`` must not hide the documents from the agent.

    Every search tool skips the state directory, which is right for the
    database and the logs and wrong for the user's own writing. The workspace
    subtree is exempted, so the agent can still grep what it just wrote.
    """
    from daino.tools.filesystem import FileTools

    workspace = service.create("Pricing", kind="research")
    service.write_artifact(workspace.id, "findings.md", "Vendor B is cheapest", author="agent")
    tools = FileTools(tmp_path)

    document = f"{workspace.folder}/findings.md"
    assert tools.read_file(document).data["content"] == "Vendor B is cheapest"
    assert document in tools.glob_files(".daino/**/*.md").data["matches"]
    assert document in [item["path"] for item in tools.grep("cheapest").data["matches"]]
    assert document in [item["path"] for item in tools.search_text("cheapest").data["matches"]]


def test_the_rest_of_the_state_directory_stays_hidden(
    service: WorkbenchService, tmp_path: Path
) -> None:
    """The exemption is the workspaces subtree, not ``.daino`` wholesale."""
    from daino.tools.filesystem import FileTools

    service.create("Pricing")
    (tmp_path / ".daino" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".daino" / "logs" / "gui.log").write_text("cheapest", encoding="utf-8")

    tools = FileTools(tmp_path)

    assert tools.glob_files(".daino/logs/*.log").data["matches"] == []
    assert tools.grep("cheapest").data["matches"] == []
    assert tools.search_text("cheapest").data["matches"] == []


def test_a_save_based_on_a_replaced_version_is_refused(
    service: WorkbenchService,
) -> None:
    """The lost update this prevents: agent rewrites, user saves, agent's work gone.

    History made that recoverable; refusing means it never happens. The digest
    the reader was given is the token, so no extra round trip is needed to find
    out what the file holds now.
    """
    workspace = service.create("Analysis")
    service.write_artifact(workspace.id, "notes.md", "the user's draft")
    opened = service.artifact(workspace.id, "notes.md").artifact.digest
    assert opened

    # The agent finishes a step and rewrites the document.
    service.write_artifact(workspace.id, "notes.md", "the agent's rewrite", author="agent")

    with pytest.raises(StaleArtifactError) as caught:
        service.write_artifact(workspace.id, "notes.md", "more of the draft", base_digest=opened)

    assert caught.value.current_digest
    assert caught.value.current_digest != opened
    # Nothing was written, so the agent's version is still there.
    assert service.artifact(workspace.id, "notes.md").content == "the agent's rewrite"

    # "Keep mine" is the same call without the token, and it goes through.
    service.write_artifact(workspace.id, "notes.md", "more of the draft")
    assert service.artifact(workspace.id, "notes.md").content == "more of the draft"


def test_a_save_against_the_current_version_goes_through(
    service: WorkbenchService,
) -> None:
    workspace = service.create("Analysis")
    service.write_artifact(workspace.id, "notes.md", "first")
    digest = service.artifact(workspace.id, "notes.md").artifact.digest

    service.write_artifact(workspace.id, "notes.md", "second", base_digest=digest)

    assert service.artifact(workspace.id, "notes.md").content == "second"


def test_pinned_revisions_survive_the_retention_cap(
    service: WorkbenchService,
) -> None:
    """A change set is an index into history, so its blobs must outlive the trim.

    Without the pin, a workspace busy enough to roll past ``MAX_REVISIONS``
    leaves its older change sets impossible to diff and impossible to reject —
    exactly the recovery the panel offers.
    """
    workspace = service.create("Analysis")
    service.write_artifact(workspace.id, "notes.md", "version 1")
    service.pin_revisions(workspace.id, "notes.md", [1])

    for index in range(2, MAX_REVISIONS + 12):
        service.write_artifact(workspace.id, "notes.md", f"version {index}")

    versions = {item.version for item in service.revisions(workspace.id, "notes.md")}
    assert 1 in versions, "the pinned revision was trimmed away"
    assert service.revision_content(workspace.id, "notes.md", 1) == "version 1"
    # Unpinned old versions are still trimmed: this is a cap, not an archive.
    assert 2 not in versions
