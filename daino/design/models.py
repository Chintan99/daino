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

from pydantic import BaseModel, Field

DesignType = Literal[
    "architecture",
    "flowchart",
    "database",
    "api_flow",
    "ui",
    "prototype",
]

DIAGRAM_TYPES: frozenset[str] = frozenset(
    {"architecture", "flowchart", "database", "api_flow"}
)


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


class DesignFrame(BaseModel):
    """A UI mock-up frame (device viewport) with nested element children."""

    id: str
    name: str = ""
    width: int = 1440
    height: int = 900
    children: list[dict[str, Any]] = Field(default_factory=list)


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
