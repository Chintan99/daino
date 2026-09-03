"""Deriving an architecture diagram from what the code actually does.

The previous version of generate-from-code drew a node per detected framework
and connected them in a line — a picture of the dependency list, not of the
system. This builds the diagram from the repository index instead:

* **Modules become groups.** Top-level source directories are the units people
  think in, and per-file nodes produce a hairball nobody reads.
* **Imports become edges.** A module that imports another depends on it, and the
  edge is weighted by how many files do — which is the difference between "these
  touch" and "these are welded together".
* **Routes, models and env vars become boundary nodes.** They are where the
  system meets the outside world, so they get their own shapes and sit at the
  edges of the layout.

What this is *not* is a call graph. Imports overstate coupling (an import used
once weighs the same as one used everywhere in the file) and miss dynamic
dispatch entirely. The result is a starting point a person corrects, and the
generated design says so in its own metadata rather than presenting itself as
ground truth.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from daino.repository.graph import module_of, resolve_import
from daino.schemas import RepositoryIndex

#: Directories that are packaging rather than architecture.
_NOISE = frozenset(
    {
        "test",
        "tests",
        "spec",
        "specs",
        "__tests__",
        "e2e",
        "docs",
        "doc",
        "examples",
        "example",
        "scripts",
        "migrations",
        "alembic",
        "node_modules",
        "dist",
        "build",
        "vendor",
        "public",
        "static",
        "assets",
    }
)

#: How many modules to draw. Past this the picture stops being readable, and a
#: diagram nobody can read is worse than none.
MAX_MODULES = 14
#: An edge carried by fewer files than this is noise at diagram scale.
MIN_EDGE_WEIGHT = 1


@dataclass(slots=True)
class Module:
    """One architectural unit: a top-level source directory, or the root."""

    name: str
    #: Files inside it, for the size the node is drawn at and for its tooltip.
    files: list[str] = field(default_factory=list)
    #: Languages present, so a node can say "python" or "typescript".
    languages: Counter[str] = field(default_factory=Counter)
    #: Set when this module declares HTTP routes.
    routes: int = 0
    #: Set when this module declares persistence models.
    models: int = 0

    @property
    def language(self) -> str:
        return self.languages.most_common(1)[0][0] if self.languages else ""

    #: How much evidence relabels a module. The model detector is a keyword
    #: match on a file summary, so one hit is not enough to call a whole module
    #: the database — a threshold is what stops the shapes becoming noise.
    EVIDENCE_THRESHOLD = 3

    @property
    def kind(self) -> str:
        """What shape to draw. Derived from evidence, not from the name."""
        routes = self.routes >= self.EVIDENCE_THRESHOLD
        models = self.models >= self.EVIDENCE_THRESHOLD
        if routes and models:
            return "service"
        if routes:
            return "api"
        if models:
            return "database"
        return "module"


#: Directory names that are packaging rather than boundaries. A project laid
#: out as ``src/api``, ``src/worker`` has its interesting seam one level down.
_TRANSPARENT = frozenset({"src", "lib", "app", "source", "packages", "apps"})
#: When one module holds this much of the code, it is the project rather than a
#: component, and the diagram descends into it.
_DOMINANT_SHARE = 0.55


def _module_of(path: str, transparent: frozenset[str] = _TRANSPARENT) -> str:
    """The architectural unit a file belongs to, at a given granularity."""
    return module_of(path, transparent)


def _granularity(paths: list[str]) -> frozenset[str]:
    """Choose which directories to look through, from how the code is spread.

    A single-package repository — everything under ``daino/`` — would otherwise
    draw exactly one node, which is a picture of nothing. So when one directory
    holds most of the code it joins the transparent set and the diagram is drawn
    from its subdirectories instead. This is the judgement a person makes
    without thinking about it when they sketch a system on a whiteboard, and it
    has to be made here or the output is useless for the most common layout of
    all.
    """
    transparent = set(_TRANSPARENT)
    for _ in range(2):  # at most two levels down; deeper stops being a diagram
        counts: Counter[str] = Counter(_module_of(path, frozenset(transparent)) for path in paths)
        if len(counts) < 2:
            biggest = next(iter(counts), "")
        else:
            ((biggest, share),) = counts.most_common(1)
            if share / max(1, sum(counts.values())) < _DOMINANT_SHARE:
                break
        leaf = biggest.rsplit("/", 1)[-1]
        if not leaf or leaf in transparent or leaf == ".":
            break
        transparent.add(leaf)
    return frozenset(transparent)


def _import_target(
    statement: str,
    importer: str,
    paths: set[str],
    modules: set[str],
    transparent: frozenset[str],
) -> str | None:
    """Which module an import statement points at, if any is recognisable.

    Only *internal* imports become edges: a diagram of what the code depends on
    from PyPI and npm is the dependency list, which the project already has and
    which says nothing about how this system is arranged.

    Resolution is delegated to the shared file-level resolver, which anchors a
    relative import at the importing file's own directory rather than at the
    repository root. On this repository the diagram is unchanged — ``daino/gui``
    is a front end that talks to the backend over HTTP and has no cross-module
    imports to draw — but a project laid out as ``src/api`` and ``src/worker``
    importing each other relatively had every one of those edges dropped.
    """
    target = resolve_import(statement, importer, paths)
    if not target:
        return None
    module = module_of(target, transparent)
    return module if module in modules else None


def analyse(
    index: RepositoryIndex,
    *,
    routes: list[dict[str, object]] | None = None,
    models: list[object] | None = None,
    env_vars: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Modules, their dependencies, and the boundaries they expose."""
    considered = [
        file.path
        for file in index.files
        if not any(part in _NOISE for part in PurePosixPath(file.path).parts)
    ]
    transparent = _granularity(considered)
    modules: dict[str, Module] = {}
    for file in index.files:
        parts = PurePosixPath(file.path).parts
        if any(part in _NOISE for part in parts):
            continue
        name = _module_of(file.path, transparent)
        module = modules.setdefault(name, Module(name=name))
        module.files.append(file.path)
        if file.language:
            module.languages[file.language] += 1

    for entry in routes or []:
        name = _module_of(str(entry.get("path", "")), transparent)
        if name in modules:
            modules[name].routes += 1
    for symbol in models or []:
        path = getattr(symbol, "path", "")
        name = _module_of(str(path), transparent)
        if name in modules:
            modules[name].models += 1

    # Biggest first, then truncate: the modules with the most code are the ones
    # a reader is orienting themselves by.
    ordered = sorted(modules.values(), key=lambda item: (-len(item.files), item.name))
    kept = {item.name for item in ordered[:MAX_MODULES]}

    weights: dict[tuple[str, str], int] = defaultdict(int)
    paths = {file.path for file in index.files}
    for file in index.files:
        source = _module_of(file.path, transparent)
        if source not in kept:
            continue
        for statement in file.imports:
            target = _import_target(statement, file.path, paths, kept, transparent)
            if target and target != source:
                weights[(source, target)] += 1

    return {
        "modules": [item for item in ordered if item.name in kept],
        "edges": [
            {"source": source, "target": target, "weight": weight}
            for (source, target), weight in sorted(weights.items(), key=lambda pair: -pair[1])
            if weight >= MIN_EDGE_WEIGHT
        ],
        "route_count": len(routes or []),
        "model_count": len(models or []),
        "env_vars": sorted({str(item.get("name", "")) for item in (env_vars or [])}),
        "dropped": [item.name for item in ordered[MAX_MODULES:]],
    }


def layout(analysis: dict[str, object]) -> tuple[list[dict], list[dict]]:
    """Place the analysis on a canvas, and return nodes and edges to create.

    Laid out in dependency layers rather than a grid: a module that nothing
    imports goes at the top, its dependencies below it, and so on. That ordering
    is the one piece of information a diagram can carry that a file list cannot,
    so it is what the geometry is spent on.
    """
    modules: list[Module] = analysis["modules"]  # type: ignore[assignment]
    edges: list[dict] = analysis["edges"]  # type: ignore[assignment]
    depth = _layers(modules, edges)

    by_layer: dict[int, list[Module]] = defaultdict(list)
    for module in modules:
        by_layer[depth[module.name]].append(module)

    nodes: list[dict] = []
    for level in sorted(by_layer):
        row = by_layer[level]
        for column, module in enumerate(row):
            # Centred per row, so the layers read as layers.
            offset = (column - (len(row) - 1) / 2) * 260
            # Shaped as a DesignNode, not as add_node's keyword arguments.
            # `DesignService.create` validates these dicts straight into the
            # model, and pydantic drops keys it does not know — so `node_type`
            # and a flat `x`/`y` were silently discarded, leaving every node
            # "default" and stacked at the origin.
            nodes.append(
                {
                    "id": _node_id(module.name),
                    "label": module.name,
                    "type": module.kind,
                    "position": {"x": float(round(offset)), "y": float(level * 170)},
                    "data": {
                        "files": len(module.files),
                        "language": module.language,
                        "routes": module.routes,
                        "models": module.models,
                        # The tooltip: enough to check the grouping is right.
                        "detail": ", ".join(sorted(module.files)[:8]),
                    },
                }
            )

    drawn = {node["id"] for node in nodes}
    connections = [
        {
            "source": _node_id(str(edge["source"])),
            "target": _node_id(str(edge["target"])),
            # The weight is the honest part: "imported by 9 files" is a
            # different claim from "imported once".
            "label": f"{edge['weight']}×" if int(edge["weight"]) > 1 else "",
        }
        for edge in edges
        if _node_id(str(edge["source"])) in drawn and _node_id(str(edge["target"])) in drawn
    ]
    return nodes, connections


def _layers(modules: list[Module], edges: list[dict]) -> dict[str, int]:
    """How deep each module sits: 0 for nothing importing it, then downward.

    Cycles are common in real code and are not an error here — a module in a
    cycle simply keeps the depth it was first assigned, which draws the cycle as
    a same-layer cluster rather than looping forever trying to order it.
    """
    names = [module.name for module in modules]
    dependents: dict[str, list[str]] = defaultdict(list)
    incoming: Counter[str] = Counter({name: 0 for name in names})
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        dependents[source].append(target)
        incoming[target] += 1

    depth = dict.fromkeys(names, 0)
    # Start from the modules nothing depends on and walk downward.
    frontier = [name for name in names if incoming[name] == 0] or names[:1]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for target in dependents.get(current, []):
            if target not in seen:
                depth[target] = max(depth[target], depth[current] + 1)
                frontier.append(target)
    return depth


def _node_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "module"


def summary(analysis: dict[str, object]) -> str:
    """One paragraph saying what was derived, and what it cannot know.

    Stored in the design's metadata so the diagram carries its own caveat: a
    generated picture that does not admit it was generated is a picture people
    trust more than they should.
    """
    modules: list[Module] = analysis["modules"]  # type: ignore[assignment]
    edges: list[dict] = analysis["edges"]  # type: ignore[assignment]
    dropped: list[str] = analysis["dropped"]  # type: ignore[assignment]
    parts = [
        f"Derived from the repository index: {len(modules)} module(s) and "
        f"{len(edges)} dependency edge(s), from import statements.",
    ]
    if analysis["route_count"]:
        parts.append(f"{analysis['route_count']} HTTP route(s) found.")
    if analysis["model_count"]:
        parts.append(f"{analysis['model_count']} persistence model(s) found.")
    if dropped:
        parts.append(
            f"{len(dropped)} smaller module(s) were left out to keep the "
            f"diagram readable: {', '.join(dropped[:6])}."
        )
    parts.append(
        "Edges come from imports, which overstate coupling and miss anything "
        "dispatched at runtime. Treat this as a starting point to correct, not "
        "as a reverse-engineered truth."
    )
    return " ".join(parts)
