"""The agent's design tools mutate the same artifact the GUI reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.design import DesignConflictError, DesignService
from daino.schemas import AgentAction
from daino.tools import EditTools
from daino.tools.editing import ActionExecutor


def _executor(root: Path) -> ActionExecutor:
    editor = EditTools(root)
    return ActionExecutor(editor, design=DesignService(root))


@pytest.mark.asyncio
async def test_agent_can_build_an_architecture_diagram(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    created, _ = await executor.execute(
        AgentAction(
            thought="scaffold",
            action="create_design",
            design_name="Project Architecture",
            design_type="architecture",
        )
    )
    assert created.success
    design_id = created.data["design_id"]

    for label in ("React Frontend", "FastAPI Backend"):
        node, _ = await executor.execute(
            AgentAction(
                thought="add",
                action="add_design_node",
                design_id=design_id,
                node_label=label,
                node_type="service",
            )
        )
        assert node.success

    nodes = created.data  # refreshed below
    connected, _ = await executor.execute(
        AgentAction(
            thought="wire",
            action="connect_design_nodes",
            design_id=design_id,
            source_node=[n["id"] for n in node.data["nodes"]][0],
            target_node=[n["id"] for n in node.data["nodes"]][1],
            edge_label="HTTP",
        )
    )
    assert connected.success
    assert len(connected.data["edges"]) == 1

    # The same artifact is visible to the GUI-facing service.
    stored = DesignService(tmp_path).get(design_id)
    assert len(stored.nodes) == 2
    assert len(stored.edges) == 1
    del nodes


@pytest.mark.asyncio
async def test_design_tool_reports_when_unavailable(tmp_path: Path) -> None:
    editor = EditTools(tmp_path)
    executor = ActionExecutor(editor)  # no design service attached
    result, _ = await executor.execute(
        AgentAction(thought="x", action="create_design", design_name="X")
    )
    assert not result.success
    assert "not available" in result.error


PAGE = "<!doctype html>\n<html><body><h1>Landing</h1></body></html>"


@pytest.mark.asyncio
async def test_agent_authors_and_rewrites_a_canvas_page(tmp_path: Path) -> None:
    """The agent can put a real HTML page on the canvas and then rewrite it."""
    executor = _executor(tmp_path)
    created, _ = await executor.execute(
        AgentAction(
            thought="scaffold",
            action="create_design",
            design_name="Canvas",
            design_type="prototype",
        )
    )
    design_id = created.data["design_id"]

    added, _ = await executor.execute(
        AgentAction(
            thought="author the page",
            action="add_design_node",
            design_id=design_id,
            node_label="Landing",
            node_kind="html",
            node_content=PAGE,
        )
    )
    assert added.success
    node = added.data["nodes"][-1]
    node_id = node["id"]
    # An artifact is a real file on the canvas, not a diagram box.
    assert node["type"] == "artifact"
    assert node["data"]["kind"] == "html"
    assert node["data"]["filename"].endswith(".html")
    assert node["data"]["width"] and node["data"]["height"]

    rewritten = PAGE.replace("Landing", "Landing v2")
    updated, _ = await executor.execute(
        AgentAction(
            thought="tweak the headline",
            action="update_design_node",
            design_id=design_id,
            node_id=node_id,
            node_content=rewritten,
        )
    )
    assert updated.success

    stored = DesignService(tmp_path).get(design_id).node(node_id)
    assert stored is not None
    assert stored.data["content"] == rewritten
    # A rewrite replaces the source and nothing else the user owns.
    assert stored.data["kind"] == "html"
    assert stored.data["filename"] == node["data"]["filename"]
    assert stored.data["width"] == node["data"]["width"]


@pytest.mark.asyncio
async def test_reading_a_design_summarises_artifact_source(tmp_path: Path) -> None:
    """Listing a design must not flood the context with page markup."""
    executor = _executor(tmp_path)
    created, _ = await executor.execute(
        AgentAction(
            thought="x",
            action="create_design",
            design_name="Canvas",
            design_type="prototype",
        )
    )
    design_id = created.data["design_id"]
    long_page = "<!doctype html><html><body>" + ("<p>filler</p>" * 400) + "</body></html>"
    added, _ = await executor.execute(
        AgentAction(
            thought="author",
            action="add_design_node",
            design_id=design_id,
            node_id="page",
            node_label="Page",
            node_kind="html",
            node_content=long_page,
        )
    )
    summarized = added.data["nodes"][-1]["data"]
    assert len(summarized["content"]) < 200
    assert summarized["content"].endswith("…")
    assert summarized["content_chars"] == len(long_page)

    # The dedicated read returns the whole thing.
    full, _ = await executor.execute(
        AgentAction(
            thought="read it",
            action="read_design_artifact",
            design_id=design_id,
            node_id="page",
        )
    )
    assert full.success
    assert full.data["content"] == long_page
    assert full.data["kind"] == "html"

    missing, _ = await executor.execute(
        AgentAction(
            thought="read it",
            action="read_design_artifact",
            design_id=design_id,
            node_id="nope",
        )
    )
    assert not missing.success
    assert "Unknown node" in missing.error


@pytest.mark.asyncio
async def test_plain_diagram_nodes_are_unaffected(tmp_path: Path) -> None:
    """A node without artifact fields stays an ordinary diagram box."""
    executor = _executor(tmp_path)
    created, _ = await executor.execute(
        AgentAction(thought="x", action="create_design", design_name="Arch")
    )
    design_id = created.data["design_id"]
    added, _ = await executor.execute(
        AgentAction(
            thought="add",
            action="add_design_node",
            design_id=design_id,
            node_label="Queue",
            node_type="queue",
        )
    )
    node = added.data["nodes"][-1]
    assert node["type"] == "queue"
    assert node["data"] == {}


def test_two_editors_cannot_silently_overwrite_each_other(tmp_path: Path) -> None:
    """The lost update this prevents.

    Both windows load version 2 and post back a whole document; without an
    expected version they both write version 3, and the second erases the
    first. A design is an hour of somebody moving nodes around — losing it
    silently is the worst outcome available.
    """
    service = DesignService(tmp_path)
    design = service.create("Architecture", "architecture")
    service.add_node(design.id, label="API")

    first = service.get(design.id)
    second = service.get(design.id)
    assert first.version == second.version

    # The first window saves.
    first.name = "Architecture (first)"
    service.replace(first, expected_version=first.version)

    # The second still holds the version it loaded.
    second.name = "Architecture (second)"
    with pytest.raises(DesignConflictError) as caught:
        service.replace(second, expected_version=second.version)

    assert caught.value.stored_version > second.version
    assert service.get(design.id).name == "Architecture (first)"


def test_every_saved_version_of_a_design_can_be_restored(tmp_path: Path) -> None:
    """ "Version" used to be a counter that overwrote its own predecessor."""
    service = DesignService(tmp_path)
    design = service.create("Flow", "flowchart")
    service.add_node(design.id, label="Start", node_id="start")
    with_two = service.add_node(design.id, label="Finish", node_id="finish")
    service.delete_node(design.id, "finish")

    assert [node.id for node in service.get(design.id).nodes] == ["start"]

    versions = [item["version"] for item in service.revisions(design.id)]
    assert with_two.version in versions

    restored = service.restore(design.id, with_two.version)

    assert [node.id for node in restored.nodes] == ["start", "finish"]
    # A restore moves forward, so undoing it is the same operation again.
    assert restored.version > with_two.version


def test_a_stale_expected_version_is_the_only_thing_refused(tmp_path: Path) -> None:
    """Callers that have genuinely just read the document keep working."""
    service = DesignService(tmp_path)
    design = service.create("Architecture", "architecture")

    current = service.get(design.id)
    current.name = "Renamed"

    # No expectation stated: last writer wins, as before.
    assert service.replace(current).name == "Renamed"


@pytest.mark.asyncio
async def test_agent_draws_a_ui_mock_up_frame(tmp_path: Path) -> None:
    """Frames are reachable from the agent, not just from the HTTP routes.

    The frame model, its endpoints and its versioning all existed while nothing
    could create a frame: the tool surface had no verb for one, so the whole
    thing was a design model with no way in.
    """
    executor = _executor(tmp_path)
    created, _ = await executor.execute(
        AgentAction(
            thought="scaffold",
            action="create_design",
            design_name="Screens",
            design_type="ui",
        )
    )
    design_id = created.data["design_id"]

    added, _ = await executor.execute(
        AgentAction(
            thought="draw the login screen",
            action="add_design_frame",
            design_id=design_id,
            frame_name="Login",
            frame_width=390,
            frame_height=844,
            frame_elements=[
                {"type": "heading", "label": "Sign in", "x": 24, "y": 80},
                {"type": "input", "label": "Email", "x": 24, "y": 160, "height": 44},
                {"type": "button", "label": "Continue", "x": 24, "y": 240, "height": 44},
            ],
        )
    )
    assert added.success, added.error
    assert added.data["frames"] == [
        {"id": "login", "name": "Login", "width": 390, "height": 844, "elements": 3}
    ]

    design = DesignService(tmp_path).get(design_id)
    element = design.frames[0].children[1]
    assert (element.type, element.label, element.y, element.height) == ("input", "Email", 160, 44)


@pytest.mark.asyncio
async def test_renaming_a_frame_keeps_what_is_drawn_in_it(tmp_path: Path) -> None:
    """An update that mentions no elements must not empty the screen.

    On the wire "no elements" and "an empty list" are the same value, so a
    rename that forwarded the empty default would delete the mock-up it was
    renaming.
    """
    service = DesignService(tmp_path)
    design = service.create("Screens", "ui")
    service.add_frame(design.id, name="Login", children=[{"type": "button", "label": "Continue"}])
    executor = ActionExecutor(EditTools(tmp_path), design=service)

    renamed, _ = await executor.execute(
        AgentAction(
            thought="clearer name",
            action="update_design_frame",
            design_id=design.id,
            frame_id="login",
            frame_name="Sign in",
        )
    )

    assert renamed.success, renamed.error
    frame = service.get(design.id).frames[0]
    assert frame.name == "Sign in"
    assert [item.label for item in frame.children] == ["Continue"]


@pytest.mark.asyncio
async def test_a_frame_can_be_deleted(tmp_path: Path) -> None:
    service = DesignService(tmp_path)
    design = service.create("Screens", "ui")
    service.add_frame(design.id, name="Login")
    executor = ActionExecutor(EditTools(tmp_path), design=service)

    removed, _ = await executor.execute(
        AgentAction(
            thought="drop it",
            action="delete_design_frame",
            design_id=design.id,
            frame_id="login",
        )
    )

    assert removed.success, removed.error
    assert removed.data["frames"] == []
    assert service.get(design.id).frames == []
