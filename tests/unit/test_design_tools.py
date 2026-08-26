"""The agent's design tools mutate the same artifact the GUI reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.design import DesignService
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
