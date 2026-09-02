"""Choosing which files a task actually needs, and in what order.

What this replaces: a case-insensitive substring match of every word longer than
three characters from the task's title and objective against ``f"{path}
{summary}"``, filled to budget in filesystem walk order. Measured on this
repository for the task *"Rank discovered files by import distance in the context
compiler"*, that matcher returns 191 files — 38% of the repository — and the
first five in walk order are ``vasuki/__init__.py``, ``tests/conftest.py`` and
three unrelated test modules. In compact mode, where only four files are packed,
the entire source budget goes to those. ``daino/context/builder.py``, the sole
caller of the file being edited, is never reached.

The fix is not a better matcher. It is to use the edges the index already holds:
start from the files the task actually names, and expand along imports. The
substring match stays as the floor, ranked last, so the candidate set is always
a superset of today's and an empty graph reproduces today's behaviour exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from daino.repository.graph import ImportGraph, is_test_path
from daino.schemas import RepositoryIndex, TaskSpec

#: Distances, smallest first. Not hop counts — a confidence ordering that
#: happens to be monotonic in hops.
#:
#: The tier that earns its place here is the barrel. ``compiler.py`` imports
#: ``daino.repository`` and ``daino.schemas``, which resolve to two ``__init__``
#: files of 239 and 1,960 bytes containing no logic at all; ``indexer.py`` and
#: ``core.py`` — the files it actually uses — are at distance 2 *through a
#: re-export*. A design that stopped at distance 1 would retrieve two empty
#: barrels and nothing else.
_DEFINES_SYMBOL = 0.9
_SEED_IMPORTS = 1.0
_IMPORTS_SEED = 1.2
_THROUGH_BARREL = 1.5
_DIRECTORY_SIBLING = 1.6
_SECOND_HOP = 2.0
#: The floor. Everything the old matcher found, after everything the graph did.
_LEXICAL = 9.0
#: Breaks ties among lexical matches by index position, which is walk order —
#: so with no graph at all the output is today's list in today's order.
_LEXICAL_TIEBREAK = 1e-4

#: Both a graph hit and a word match is better evidence than either alone.
_ALSO_LEXICAL_BONUS = 0.25
#: Reached from several seeds independently: more likely to be the shared piece.
_PER_EXTRA_SEED_BONUS = 0.05
_MAX_SEED_BONUS = 0.20
#: A test file that is not the subject's own test is background, not context.
_UNRELATED_TEST_PENALTY = 0.3
#: A subject's own test sits just behind the subject.
_TEST_OF_SUBJECT_OFFSET = 0.05

#: Seeding from 191 substring hits would expand to the whole repository and mean
#: nothing. The seeds are meant to be the few files the task is really about.
MAX_SEEDS = 8
#: How many lexical matches may seed, and only when nothing better exists.
_LEXICAL_SEEDS = 5

_PATH_MARKERS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".css",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".md",
)


def lexical_matches(index: RepositoryIndex, task: TaskSpec) -> list[str]:
    """Today's matcher, extracted verbatim.

    Kept exactly as it was — same word length, same case folding, same haystack,
    same order — so that the ranked result is provably a superset of what the
    compiler produced before, and the floor cannot regress.
    """
    terms = {word.lower() for word in f"{task.title} {task.objective}".split() if len(word) > 3}
    return [
        item.path
        for item in index.files
        if any(term in f"{item.path} {item.summary}".lower() for term in terms)
    ]


@dataclass(frozen=True)
class Candidate:
    """One file worth showing the agent, and how near the task it is."""

    path: str
    score: float
    reason: str

    @property
    def near(self) -> bool:
        """Close enough that its absence from the bundle is worth reporting."""
        return self.score <= _IMPORTS_SEED


def select_candidates(
    index: RepositoryIndex,
    task: TaskSpec,
    required: list[str],
    graph: ImportGraph,
) -> list[Candidate]:
    """Rank every file the task might need, nearest first.

    ``required`` is the task's own scope, which the compiler includes separately
    and unconditionally; it is passed here to seed the expansion, not to be
    returned.
    """
    lexical = lexical_matches(index, task)
    lexical_rank = {path: position for position, path in enumerate(lexical)}
    seeds, unindexed = _seeds(task, required, lexical, graph)

    # path -> (distance, reason, how many seeds reached it independently)
    found: dict[str, tuple[float, str, int]] = {}

    def offer(path: str, distance: float, reason: str, *, reaches: bool = True) -> None:
        if path not in graph.paths:
            return
        previous = found.get(path)
        reach = (previous[2] if previous else 0) + (1 if reaches else 0)
        if previous is None or distance < previous[0]:
            found[path] = (distance, reason, reach)
        else:
            found[path] = (previous[0], previous[1], reach)

    for symbol in task.relevant_symbols:
        for path in graph.defines(symbol):
            offer(path, _DEFINES_SYMBOL, f"defines {symbol}")

    for seed in seeds:
        for neighbour in graph.neighbours(seed):
            if neighbour.relation == "imports":
                offer(neighbour.path, _SEED_IMPORTS, f"imported by {seed}")
                if graph.is_barrel(neighbour.path):
                    # The re-export itself carries no logic; what it exposes does.
                    for behind in sorted(graph.imports.get(neighbour.path, ())):
                        offer(behind, _THROUGH_BARREL, f"re-exported by {neighbour.path}")
                else:
                    for onward in sorted(graph.imports.get(neighbour.path, ())):
                        offer(onward, _SECOND_HOP, f"used by {neighbour.path}")
            else:
                offer(neighbour.path, _IMPORTS_SEED, f"imports {seed}")

    # Note what is deliberately absent: an *indexed* file with no edges does not
    # nominate its directory neighbours. Having no edges is itself information —
    # nothing depends on it and it depends on nothing — and treating proximity on
    # disk as relevance pulls in whatever else happens to sit in that folder.
    for seed in unindexed:
        # A scoped file the index has never seen: the task is creating it, which
        # is the ordinary case for a planned change. It has no edges by
        # definition, so its directory is the only structural evidence there is —
        # and the neighbours it will sit among are exactly what the agent needs
        # to match the conventions of the place it is writing into.
        for sibling in sorted(graph.siblings(seed)):
            offer(sibling, _DIRECTORY_SIBLING, f"beside {seed}")

    for path in lexical:
        offer(
            path,
            _LEXICAL + _LEXICAL_TIEBREAK * lexical_rank[path],
            "matches the task text",
            reaches=False,
        )

    scoped = set(required)
    # A file's own test follows it rather than sitting in a block of its own, so
    # the two stay adjacent in the packing order and a budget that admits the
    # file admits its test. This also preserves the compiler's old stem
    # matching, which found tests the graph cannot: a Go test, or a Python one
    # that reaches its subject only through a fixture, imports nothing.
    subjects = scoped | {
        path for path, (distance, _, _) in found.items()
        if distance < _LEXICAL and not is_test_path(path)
    }
    for file in index.files:
        if not is_test_path(file.path):
            continue
        subject = _subject_of(file.path, subjects)
        if subject:
            anchor = found[subject][0] if subject in found else _SEED_IMPORTS
            offer(
                file.path,
                anchor + _TEST_OF_SUBJECT_OFFSET,
                f"tests {subject}",
                reaches=False,
            )

    ranked: list[Candidate] = []
    for path, (distance, reason, reach) in found.items():
        if path in scoped:
            continue  # already mandatory; the compiler adds it first
        score = distance
        if distance < _LEXICAL and path in lexical_rank:
            score -= _ALSO_LEXICAL_BONUS
        score -= min(_MAX_SEED_BONUS, _PER_EXTRA_SEED_BONUS * max(0, reach - 1))
        if is_test_path(path) and not reason.startswith("tests "):
            # A test of something else entirely is background, not context.
            score += _UNRELATED_TEST_PENALTY
        ranked.append(Candidate(path=path, score=score, reason=reason))
    # Path breaks every tie, so the order is fully deterministic — the packing
    # that follows is budget-sensitive, and a wobbling order would make which
    # files the agent sees depend on dictionary iteration.
    ranked.sort(key=lambda item: (item.score, item.path))
    return ranked


def _seeds(
    task: TaskSpec,
    required: list[str],
    lexical: list[str],
    graph: ImportGraph,
) -> tuple[list[str], list[str]]:
    """The few files the task is really about, and those not yet indexed.

    Lexical matches seed only when nothing better exists, and never a test file.
    Expanding from 191 substring hits reaches the whole repository, which is the
    failure this module was written to fix rather than a fallback for it — and a
    test file as a seed contributes whatever that test happens to import, which
    is an arbitrary slice of the project.
    """
    seeds: list[str] = [path for path in required if path in graph.paths]
    # A scoped path with no index entry is one the task will create. It cannot
    # be expanded from, but it still says where the work is happening.
    unindexed = [
        path for path in required if path not in graph.paths and "*" not in path
    ][:MAX_SEEDS]
    if len(seeds) < MAX_SEEDS:
        seeds.extend(path for path in _named_paths(task, graph) if path not in seeds)
    if not seeds and not unindexed:
        seeds.extend(path for path in lexical if not is_test_path(path))
        seeds = seeds[:_LEXICAL_SEEDS]
    return list(dict.fromkeys(seeds))[:MAX_SEEDS], unindexed


def _named_paths(task: TaskSpec, graph: ImportGraph) -> list[str]:
    """Paths written out in the task's own prose.

    On the chat path this is the only seed available: ``_team_context``
    fabricates a ``TaskSpec`` with no ``expected_files`` and no
    ``allowed_files``, so every scrap of grounding comes from what the user
    actually typed.
    """
    found: list[str] = []
    for raw in f"{task.title} {task.objective}".split():
        token = raw.strip("`'\"(),;:[]{}<>")
        if not token or ("/" not in token and not token.endswith(_PATH_MARKERS)):
            continue
        if token in graph.paths:
            found.append(token)
            continue
        matches = [path for path in graph.paths if path.endswith(f"/{token}")]
        if len(matches) == 1:
            # Only when unambiguous: "service.py" names eleven files here, and
            # picking one of them arbitrarily is worse than picking none.
            found.append(matches[0])
    return list(dict.fromkeys(found))


def _subject_of(test_path: str, subjects: set[str]) -> str:
    """The file a test is about, when one of the candidates plainly is it."""
    stem = test_path.rsplit("/", 1)[-1]
    for suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rb"):
        stem = stem.removesuffix(suffix)
    stem = stem.removeprefix("test_").removesuffix("_test").removesuffix(".spec")
    stem = stem.removesuffix(".test")
    # A package file has no distinguishing stem: `tests/__init__.py` would
    # otherwise claim to be the test of every `__init__.py` in the repository.
    if len(stem) < 3 or stem in {"__init__", "index", "main", "conftest"}:
        return ""
    for subject in sorted(subjects):
        name = subject.rsplit("/", 1)[-1]
        for suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rb"):
            name = name.removesuffix(suffix)
        if name == stem:
            return subject
    return ""
