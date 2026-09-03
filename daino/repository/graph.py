"""The import graph the index already contains but nothing ever read.

``RepositoryFile.imports`` is collected for every file at index time and, until
now, used only to draw the architecture diagram. This turns those statements
into file-level edges: which file imports which, and — built in the same pass —
which files import a given one.

Pure functions over an already-loaded ``RepositoryIndex``. No disk access, no
subprocesses, no model calls. Building the graph for a 500-file repository costs
about six milliseconds, against the three the index's own JSON parse costs, so
there is nothing here worth caching.

Resolution is membership-tested against the index, which makes the graph
structurally incapable of nominating a path that does not exist. A language
whose outline extractor does not collect imports simply has no edges; every
caller degrades to whatever it did before rather than breaking.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from daino.schemas import RepositoryIndex, RepositorySymbol

#: Suffixes an import may resolve to, in preference order. A bare
#: ``./components/Button`` means ``Button.tsx`` before ``Button/index.ts``.
_SOURCE_SUFFIXES = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)

#: Files that stand for a directory when an import names the directory itself.
_PACKAGE_FILES = ("__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx")

#: A file importing more than this many others is a package entry point rather
#: than a module: expanding through it reaches most of the repository and says
#: nothing about what belongs with what.
BARREL_FANOUT_LIMIT = 12

#: A file imported by more than this many others — a schemas package, a settings
#: module — is a hub. "Everything that imports it" is the repository, not a
#: signal, so reverse expansion stops there.
HUB_FANIN_LIMIT = 25

#: A symbol defined in more than this many files is a common name (``run``,
#: ``main``, ``build``) rather than an identifying one.
_AMBIGUOUS_SYMBOL_LIMIT = 5


def is_test_path(path: str) -> bool:
    """Whether a path is test code.

    One predicate, because three copies of it had already drifted: the indexer
    matched on the file name and any ``tests`` path component, the compiler
    matched on the word "test" anywhere in the resolved name, and the stem
    matcher removed a ``test_`` prefix. A file counted as a test by one and not
    by another lands in the wrong half of a bundle.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1].lower()
    return "test" in name or any(
        part.lower() in {"tests", "test", "__tests__", "spec", "specs"} for part in parts
    )


def module_of(path: str, transparent: frozenset[str]) -> str:
    """The architectural unit a file belongs to, at a given granularity.

    ``transparent`` names the directories to look *through*. It is a parameter
    rather than a constant because the right granularity depends on the
    repository — see ``daino.design.architecture._granularity``, which is the
    only caller that varies it.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return "."
    if len(parts) > 2 and parts[0] in transparent:
        return f"{parts[0]}/{parts[1]}"
    if len(parts) == 1:
        return "."
    return parts[0]


def import_fragment(statement: str, importer: str) -> str:
    """Normalise one import statement to a repository-relative path fragment.

    Relative imports are anchored at the *importing file's* directory, which is
    the whole point. The architecture diagram's own resolver strips leading
    ``./`` and ``../`` and resolves what is left against the repository root, so
    ``../../api/client`` becomes ``api/client`` and matches nothing. That drops
    every relative import there is — on this repository, all 521 of
    ``daino/gui``'s, which is the entire front end's structure.
    """
    text = statement.strip().strip("'\"")
    if not text:
        return ""
    if not text.startswith("."):
        # Absolute: a Python dotted module, or a bare specifier already written
        # as a path. Neither is anchored anywhere but the repository root.
        return text if "/" in text else text.replace(".", "/")
    base = PurePosixPath(importer).parent
    if "/" in text:
        # A JS/TS specifier: "./x", "../../api/client". Walked segment by
        # segment because "../../" is two levels up, while the leading dot run
        # is only two characters — counting dots resolves every specifier past
        # the first level one directory too deep.
        segments = text.split("/")
    else:
        # A relative Python import. The indexer records `from .models import X`
        # as the bare name "models" — `node.level` is dropped — so leading dots
        # rarely reach here. Handled anyway, so that a future re-index which
        # does preserve them resolves more precisely without another change.
        depth = len(text) - len(text.lstrip("."))
        segments = [".."] * (depth - 1) + text[depth:].split(".")
    for segment in segments:
        if segment in ("", "."):
            continue
        base = base.parent if segment == ".." else base / segment
    resolved = str(base)
    return "" if resolved in (".", "/", "") else resolved.removeprefix("./")


def resolve_import(statement: str, importer: str, paths: set[str]) -> str:
    """The indexed file an import statement points at, or "".

    Membership in *paths* is what makes an edge: an import that resolves to
    nothing in the index is external (stdlib, PyPI, npm) and is dropped. On this
    repository that is 1,443 statements, of which zero are internal — the
    resolution has full recall on what it is meant to catch.
    """
    fragment = import_fragment(statement, importer)
    if not fragment:
        return ""
    if fragment in paths:
        return fragment
    for suffix in _SOURCE_SUFFIXES:
        candidate = f"{fragment}{suffix}"
        if candidate in paths:
            return candidate
    for package in _PACKAGE_FILES:
        candidate = f"{fragment}/{package}"
        if candidate in paths:
            return candidate
    # A Python import names a module, not a file: ``daino.repository`` is the
    # package's __init__. Anything deeper is handled by the loop above.
    return ""


@dataclass(frozen=True)
class Neighbour:
    """One file reachable from another, and how."""

    path: str
    relation: str


@dataclass
class ImportGraph:
    """Who imports whom, plus what defines which symbol.

    Both directions are built in the same pass because the reverse edges are the
    exact transpose and computing them twice is the only way they can disagree.
    """

    #: path -> files it imports
    imports: dict[str, set[str]] = field(default_factory=dict)
    #: path -> files that import it
    imported_by: dict[str, set[str]] = field(default_factory=dict)
    #: symbol name -> files defining it
    definitions: dict[str, set[str]] = field(default_factory=dict)
    #: every indexed path, for membership tests
    paths: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, index: RepositoryIndex) -> ImportGraph:
        paths = {file.path for file in index.files}
        imports: dict[str, set[str]] = defaultdict(set)
        imported_by: dict[str, set[str]] = defaultdict(set)
        definitions: dict[str, set[str]] = defaultdict(set)
        for file in index.files:
            for statement in file.imports:
                target = resolve_import(statement, file.path, paths)
                if target and target != file.path:
                    imports[file.path].add(target)
                    imported_by[target].add(file.path)
            for symbol in file.symbols:
                definitions[symbol.name].add(file.path)
        return cls(
            imports=dict(imports),
            imported_by=dict(imported_by),
            definitions=dict(definitions),
            paths=paths,
        )

    def is_barrel(self, path: str) -> bool:
        """A package entry point: named like one, and re-exporting like one."""
        name = PurePosixPath(path).name
        return name in _PACKAGE_FILES and len(self.imports.get(path, ())) <= BARREL_FANOUT_LIMIT

    def defines(self, symbol: str) -> set[str]:
        """Files defining *symbol*, or nothing when the name is too common.

        ``run``, ``main`` and ``build`` are defined in every third file and are
        evidence of nothing. Returning them would swamp the ranking with the
        files that happen to have the most ordinary names in them.
        """
        found = self.definitions.get(symbol, set())
        if len(found) > _AMBIGUOUS_SYMBOL_LIMIT:
            return set()
        return found

    def siblings(self, path: str) -> set[str]:
        """Files in the same directory.

        The fallback for a seed with no edges at all — a file about to be
        created, which is the common case when a planner scopes a new module.
        """
        parent = str(PurePosixPath(path).parent)
        return {
            other
            for other in self.paths
            if other != path and str(PurePosixPath(other).parent) == parent
        }

    def neighbours(self, path: str) -> list[Neighbour]:
        """Everything one hop from *path*, labelled with how it was reached.

        Reverse edges are distance 1, not 2: a file that imports the seed is as
        close to it as one the seed imports. What differs is confidence, and
        that belongs to the ranking rather than to the graph.
        """
        found: list[Neighbour] = [
            Neighbour(target, "imports") for target in sorted(self.imports.get(path, ()))
        ]
        importers = self.imported_by.get(path, set())
        if len(importers) <= HUB_FANIN_LIMIT:
            found.extend(Neighbour(source, "imported-by") for source in sorted(importers))
        return found


def symbols_by_name(index: RepositoryIndex) -> dict[str, list[RepositorySymbol]]:
    """Every indexed symbol, grouped by name.

    Deliberately *not* ``RepositoryIndexer.find_symbol``, which re-loads the
    whole index from disk on every call. Nor ``find_references``, which reads and
    regex-scans every indexed file — 500 file reads inside a function that would
    run on every agent turn. Neither belongs anywhere near retrieval.
    """
    grouped: dict[str, list[RepositorySymbol]] = defaultdict(list)
    for file in index.files:
        for symbol in file.symbols:
            grouped[symbol.name].append(symbol)
    return dict(grouped)
