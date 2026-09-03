"""Pydantic models for Daino design artifacts.

A design is a structured, versioned document — nodes and edges for diagram types
(architecture, flowchart, database, API flow) and frames for UI mock-ups — that
both the agent (via granular tools) and the user (via the React Flow canvas)
edit. The schema is intentionally permissive (`extra` data bags) so new node or
frame properties can be added without a migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DesignType = Literal[
    "architecture",
    "flowchart",
    "database",
    "api_flow",
    "ui",
    "prototype",
]

DIAGRAM_TYPES: frozenset[str] = frozenset({"architecture", "flowchart", "database", "api_flow"})


class Position(BaseModel):
    x: float = 0.0
    y: float = 0.0


class DesignNode(BaseModel):
    id: str
    label: str = ""
    #: Free-form node kind (e.g. "service", "database", "queue", "table").
    type: str = "default"
    position: Position = Field(default_factory=Position)
    #: Arbitrary node payload — table columns, endpoint details, styling, etc.
    data: dict[str, Any] = Field(default_factory=dict)


class DesignEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


#: What a frame element can be. Deliberately short: these are the shapes a
#: mock-up is actually made of, and a longer list would be a component library
#: rather than a wireframe.
FrameElementType = Literal[
    "box",
    "text",
    "heading",
    "button",
    "input",
    "image",
    "list",
    "nav",
]


class DesignFrameElement(BaseModel):
    """One element inside a UI mock-up frame.

    Typed rather than a bare dict, because a shape nothing agrees on is a shape
    nothing can draw: the frame endpoints accepted arbitrary children and the
    canvas rendered none of them, so the whole frame model sat unused. Extra
    keys are still allowed and preserved, so an editor may carry styling this
    does not name without needing a migration — the same bargain the node
    ``data`` bag makes.

    Coordinates are relative to whatever contains the element — the frame for a
    top-level element, the parent for a nested one — in the frame's own pixels.
    Relative rather than absolute so moving a container moves what is inside it,
    which is what nesting is for; and in frame pixels so a mock-up means the
    same thing whatever scale it is rendered at.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    type: FrameElementType = "box"
    #: The visible text: a button's caption, a field's placeholder, a heading.
    label: str = ""
    x: int = 0
    y: int = 0
    width: int = 200
    height: int = 48
    children: list[DesignFrameElement] = Field(default_factory=list)


class DesignFrame(BaseModel):
    """A UI mock-up frame (device viewport) with nested element children."""

    id: str
    name: str = ""
    width: int = 1440
    height: int = 900
    children: list[DesignFrameElement] = Field(default_factory=list)


class Design(BaseModel):
    id: str
    name: str
    type: DesignType = "architecture"
    version: int = 1
    nodes: list[DesignNode] = Field(default_factory=list)
    edges: list[DesignEdge] = Field(default_factory=list)
    frames: list[DesignFrame] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def node(self, node_id: str) -> DesignNode | None:
        return next((item for item in self.nodes if item.id == node_id), None)

    def summary(self) -> DesignSummary:
        return DesignSummary(
            id=self.id,
            name=self.name,
            type=self.type,
            version=self.version,
            node_count=len(self.nodes),
            edge_count=len(self.edges),
            frame_count=len(self.frames),
            updated_at=self.updated_at,
        )


class DesignSummary(BaseModel):
    id: str
    name: str
    type: DesignType
    version: int
    node_count: int
    edge_count: int
    frame_count: int
    updated_at: datetime
