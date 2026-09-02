"""Load, persist, and mutate design artifacts under ``.daino/designs``.

Both the agent's design tools and the GUI's REST/canvas operations go through
this single service, so a manual edit and an AI edit mutate the *same* stored
document. Mutations bump ``version`` and (when a bus is attached) publish
``DesignUpdated`` so connected GUIs refresh.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daino.config import paths
from daino.design.models import (
    Design,
    DesignEdge,
    DesignFrame,
    DesignNode,
    DesignSummary,
)
from daino.events import DesignCreated, DesignUpdated, EventBus

#: How many versions of one design are kept on disk. Deep enough that an
#: afternoon's editing is recoverable, bounded so a canvas nobody prunes cannot
#: fill the state directory.
MAX_DESIGN_REVISIONS = 100


class DesignError(Exception):
    """Raised for missing designs or invalid design mutations."""


class DesignConflictError(DesignError):
    """Raised when a save is based on a version the design no longer holds.

    Carries the version actually stored so a caller can say what it collided
    with instead of only that it collided.
    """

    def __init__(self, message: str, *, stored_version: int = 0) -> None:
        super().__init__(message)
        self.stored_version = stored_version


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "design"


class DesignService:
    def __init__(self, root: Path, *, events: EventBus | None = None) -> None:
        self.root = Path(root).resolve()
        self.events = events

    # --- storage locations -------------------------------------------------
    def _designs_dir(self) -> Path:
        return paths.state_dir(self.root, create=True) / "designs"

    def _design_dir(self, design_id: str) -> Path:
        return self._designs_dir() / design_id

    def _design_file(self, design_id: str) -> Path:
        return self._design_dir(design_id) / "design.json"

    def _history_dir(self, design_id: str, *, create: bool = False) -> Path:
        directory = self._design_dir(design_id) / "history"
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def prototype_dir(self, design_id: str, *, create: bool = False) -> Path:
        directory = self._design_dir(design_id) / "prototype"
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    # --- queries -----------------------------------------------------------
    def list_designs(self) -> list[DesignSummary]:
        base = self._designs_dir()
        if not base.exists():
            return []
        summaries: list[DesignSummary] = []
        for child in sorted(base.iterdir()):
            file = child / "design.json"
            if file.is_file():
                try:
                    summaries.append(self._read(file).summary())
                except (OSError, ValueError):
                    continue
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries

    def get(self, design_id: str) -> Design:
        file = self._design_file(design_id)
        if not file.is_file():
            raise DesignError(f"Unknown design {design_id!r}")
        return self._read(file)

    def exists(self, design_id: str) -> bool:
        return self._design_file(design_id).is_file()

    @staticmethod
    def _read(file: Path) -> Design:
        return Design.model_validate_json(file.read_text(encoding="utf-8"))

    # --- mutations ---------------------------------------------------------
    def create(
        self,
        name: str,
        design_type: str = "architecture",
        *,
        design_id: str | None = None,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
        frames: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> Design:
        identifier = design_id or self._unique_id(_slugify(name))
        if self.exists(identifier):
            raise DesignError(f"Design {identifier!r} already exists")
        design = Design(
            id=identifier,
            name=name,
            type=design_type,  # type: ignore[arg-type]
            nodes=[DesignNode.model_validate(node) for node in (nodes or [])],
            edges=[DesignEdge.model_validate(edge) for edge in (edges or [])],
            frames=[DesignFrame.model_validate(frame) for frame in (frames or [])],
            metadata=metadata or {},
        )
        self._write(design)
        # Version 1 is a version: without this the first state of a design is
        # the only one that could never be restored.
        self._snapshot(design)
        if self.events is not None:
            self.events.publish(
                DesignCreated(design_id=design.id, name=design.name, design_type=design.type)
            )
        return design

    def _unique_id(self, base: str) -> str:
        candidate = base
        suffix = 2
        while self.exists(candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def replace(self, design: Design, *, expected_version: int | None = None) -> Design:
        """Persist a full design document (used by manual canvas saves).

        ``expected_version`` is optimistic concurrency: the version the editor
        loaded before the user started moving nodes. When the stored design has
        moved on — a second window, or the agent finishing a design tool call —
        the save is refused rather than silently erasing that work. Passing
        ``None`` keeps the old last-writer-wins behaviour for callers that have
        genuinely just read the document.
        """
        if not self.exists(design.id):
            raise DesignError(f"Unknown design {design.id!r}")
        stored = self.get(design.id)
        if expected_version is not None and stored.version != expected_version:
            raise DesignConflictError(
                f"{design.name!r} was saved by someone else while you were editing "
                f"(you have version {expected_version}, this is now version "
                f"{stored.version}). Reload it, then reapply your change.",
                stored_version=stored.version,
            )
        # The canvas posts back a whole document; its own idea of the version is
        # the one it loaded, so the counter advances from what is stored.
        return self._save(design.model_copy(update={"version": stored.version}), change="replace")

    # --- revisions ---------------------------------------------------------
    def revisions(self, design_id: str) -> list[dict[str, Any]]:
        """Every kept version of this design, newest first.

        A design is a document people edit for hours; "version" being a counter
        that overwrote its own predecessor meant the number described nothing
        anyone could go back to.
        """
        if not self.exists(design_id):
            raise DesignError(f"Unknown design {design_id!r}")
        directory = self._history_dir(design_id)
        if not directory.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for file in directory.glob("*.json"):
            try:
                version = int(file.stem)
            except ValueError:
                continue
            found.append(
                {
                    "version": version,
                    "bytes": file.stat().st_size,
                    "saved_at": datetime.fromtimestamp(file.stat().st_mtime, UTC).isoformat(),
                }
            )
        found.sort(key=lambda item: item["version"], reverse=True)
        return found

    def revision(self, design_id: str, version: int) -> Design:
        """One kept version, exactly as it was saved."""
        file = self._history_dir(design_id) / f"{int(version)}.json"
        if not file.is_file():
            raise DesignError(f"Design {design_id!r} has no version {version}")
        return self._read(file)

    def restore(self, design_id: str, version: int) -> Design:
        """Put an old version back as the newest one.

        Restoring moves forward rather than backward: the current document is
        snapshotted first, so undoing a restore is the same operation again.
        """
        old = self.revision(design_id, version)
        current = self.get(design_id)
        return self._save(
            old.model_copy(update={"id": current.id, "version": current.version}),
            change="restore",
        )

    def add_node(
        self,
        design_id: str,
        *,
        label: str,
        node_type: str = "default",
        node_id: str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        data: dict | None = None,
    ) -> Design:
        design = self.get(design_id)
        identifier = node_id or self._unique_node_id(design, _slugify(label) or "node")
        if design.node(identifier) is not None:
            raise DesignError(f"Node {identifier!r} already exists")
        design.nodes.append(
            DesignNode(
                id=identifier,
                label=label,
                type=node_type,
                position={"x": x, "y": y},  # type: ignore[arg-type]
                data=data or {},
            )
        )
        return self._save(design, change="add_node")

    @staticmethod
    def _unique_node_id(design: Design, base: str) -> str:
        existing = {node.id for node in design.nodes}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def update_node(
        self,
        design_id: str,
        node_id: str,
        *,
        label: str | None = None,
        node_type: str | None = None,
        x: float | None = None,
        y: float | None = None,
        data: dict | None = None,
    ) -> Design:
        design = self.get(design_id)
        node = design.node(node_id)
        if node is None:
            raise DesignError(f"Unknown node {node_id!r}")
        if label is not None:
            node.label = label
        if node_type is not None:
            node.type = node_type
        if x is not None:
            node.position.x = x
        if y is not None:
            node.position.y = y
        if data is not None:
            node.data.update(data)
        return self._save(design, change="update_node")

    def delete_node(self, design_id: str, node_id: str) -> Design:
        design = self.get(design_id)
        if design.node(node_id) is None:
            raise DesignError(f"Unknown node {node_id!r}")
        design.nodes = [node for node in design.nodes if node.id != node_id]
        # Drop edges that referenced the removed node.
        design.edges = [
            edge for edge in design.edges if node_id not in (edge.source, edge.target)
        ]
        return self._save(design, change="delete_node")

    def connect(
        self,
        design_id: str,
        source: str,
        target: str,
        *,
        label: str = "",
        edge_id: str | None = None,
    ) -> Design:
        design = self.get(design_id)
        if design.node(source) is None or design.node(target) is None:
            raise DesignError("Both source and target nodes must exist")
        identifier = edge_id or self._unique_edge_id(design, f"{source}-{target}")
        design.edges.append(DesignEdge(id=identifier, source=source, target=target, label=label))
        return self._save(design, change="connect")

    @staticmethod
    def _unique_edge_id(design: Design, base: str) -> str:
        existing = {edge.id for edge in design.edges}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def disconnect(
        self,
        design_id: str,
        *,
        edge_id: str | None = None,
        source: str | None = None,
        target: str | None = None,
    ) -> Design:
        design = self.get(design_id)
        before = len(design.edges)
        if edge_id is not None:
            design.edges = [edge for edge in design.edges if edge.id != edge_id]
        elif source is not None and target is not None:
            design.edges = [
                edge
                for edge in design.edges
                if not (edge.source == source and edge.target == target)
            ]
        else:
            raise DesignError("Provide edge_id or both source and target")
        if len(design.edges) == before:
            raise DesignError("No matching edge to disconnect")
        return self._save(design, change="disconnect")

    # --- frames -----------------------------------------------------------
    def add_frame(
        self,
        design_id: str,
        *,
        name: str = "",
        frame_id: str | None = None,
        width: int = 1440,
        height: int = 900,
        children: list[dict] | None = None,
    ) -> Design:
        """Add a UI mock-up frame — a device viewport with nested elements."""
        design = self.get(design_id)
        identifier = frame_id or self._unique_frame_id(design, _slugify(name) or "frame")
        if any(frame.id == identifier for frame in design.frames):
            raise DesignError(f"Frame {identifier!r} already exists")
        design.frames.append(
            DesignFrame(
                id=identifier,
                name=name,
                width=width,
                height=height,
                children=list(children or []),
            )
        )
        return self._save(design, change="add_frame")

    @staticmethod
    def _unique_frame_id(design: Design, base: str) -> str:
        existing = {frame.id for frame in design.frames}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def update_frame(
        self,
        design_id: str,
        frame_id: str,
        *,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        children: list[dict] | None = None,
    ) -> Design:
        """Change a frame. ``children`` replaces the list rather than merging it.

        Replacing is right for a frame's children where merging is right for a
        node's data bag: a frame's children are an ordered tree the editor owns
        wholesale, and a merge would make removing an element impossible.
        """
        design = self.get(design_id)
        frame = next((item for item in design.frames if item.id == frame_id), None)
        if frame is None:
            raise DesignError(f"Unknown frame {frame_id!r}")
        if name is not None:
            frame.name = name
        if width is not None:
            frame.width = width
        if height is not None:
            frame.height = height
        if children is not None:
            frame.children = list(children)
        return self._save(design, change="update_frame")

    def delete_frame(self, design_id: str, frame_id: str) -> Design:
        design = self.get(design_id)
        if not any(frame.id == frame_id for frame in design.frames):
            raise DesignError(f"Unknown frame {frame_id!r}")
        design.frames = [frame for frame in design.frames if frame.id != frame_id]
        return self._save(design, change="delete_frame")

    def delete(self, design_id: str) -> None:
        directory = self._design_dir(design_id)
        if not directory.exists():
            raise DesignError(f"Unknown design {design_id!r}")
        import shutil

        shutil.rmtree(directory)

    # --- persistence helpers ----------------------------------------------
    def _save(self, design: Design, *, change: str) -> Design:
        design.version += 1
        design.updated_at = datetime.now(UTC)
        self._write(design)
        self._snapshot(design)
        if self.events is not None:
            self.events.publish(
                DesignUpdated(
                    design_id=design.id,
                    name=design.name,
                    design_type=design.type,
                    version=design.version,
                    change=change,
                )
            )
        return design

    def _snapshot(self, design: Design) -> None:
        """Keep this version alongside the live document.

        Best effort: a design that cannot be archived is still a design that
        saved, and failing the user's edit because the history directory is
        read-only would be the wrong trade.
        """
        try:
            directory = self._history_dir(design.id, create=True)
            (directory / f"{design.version}.json").write_text(
                json.dumps(design.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
            self._trim_history(directory)
        except OSError:
            return

    @staticmethod
    def _trim_history(directory: Path) -> None:
        """Bound the archive, oldest first."""
        versions: list[tuple[int, Path]] = []
        for file in directory.glob("*.json"):
            try:
                versions.append((int(file.stem), file))
            except ValueError:
                continue
        versions.sort()
        for _version, file in versions[:-MAX_DESIGN_REVISIONS]:
            with contextlib.suppress(OSError):
                file.unlink()

    def _write(self, design: Design) -> None:
        file = self._design_file(design.id)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps(design.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
