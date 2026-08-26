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
