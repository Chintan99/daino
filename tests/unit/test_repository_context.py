from __future__ import annotations

from pathlib import Path

from daino.context import ContextCompiler
from daino.repository import RepositoryIndexer
from daino.repository.syntax import extract_outline
from daino.schemas import TaskSpec


def test_repository_index_queries_and_incremental_reuse(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import os\nfrom fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/items')\n"
        "def list_items():\n"
        "    return os.getenv('ITEM_MODE')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import list_items\n\ndef test_items():\n    assert list_items() is None\n",
        encoding="utf-8",
    )
    indexer = RepositoryIndexer(tmp_path)
    first = indexer.build()
    second = indexer.build()
    assert len(first.files) == len(second.files) == 2
    assert indexer.find_symbol("list_items")[0].path == "app.py"
    assert indexer.api_routes()[0]["route"] == "/items"
    assert indexer.tests() == ["tests/test_app.py"]
    assert indexer.environment_variables()[0]["name"] == "ITEM_MODE"
    assert "FastAPI" in first.frameworks


def test_context_compiler_respects_budget_and_relevance(tmp_path: Path) -> None:
    (tmp_path / "invoice.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("x = 'x' * 1000\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    task = TaskSpec(
        id="task",
        title="Update invoice total",
        objective="Change invoice total",
        expected_files=["invoice.py"],
        acceptance_criteria=["total is updated"],
        verification_commands=["pytest"],
    )
    bundle = ContextCompiler(tmp_path, indexer, token_budget=200).compile(task)
    assert "invoice.py" in bundle.files
    assert "unrelated.py" not in bundle.files
    assert bundle.token_estimate <= 200


def test_tree_sitter_extracts_typescript_symbols() -> None:
    outline = extract_outline(
        "service.ts",
        b"export class InvoiceService { total(): number { return 1; } }\n",
    )
    assert outline is not None
    assert outline.parser == "tree-sitter:typescript"
    assert any(item.name == "InvoiceService" for item in outline.symbols)


def test_crash_logging_captures_a_native_fault(tmp_path: Path) -> None:
    """A segfault kills the interpreter with no traceback unless this is installed."""
    import subprocess
    import sys

    script = (
        "from pathlib import Path\n"
        "from daino.utils import crashlog\n"
        f"crashlog.install(Path({str(tmp_path)!r}))\n"
        "import ctypes\n"
        "def boom():\n"
        "    ctypes.string_at(0)\n"
        "boom()\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)

    assert result.returncode != 0
    log = (tmp_path / ".daino" / "logs" / "crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error: Segmentation fault" in log
    # The point of the log is that it names the line, not just the signal.
    assert "in boom" in log


def test_only_bundled_grammars_are_used(tmp_path: Path) -> None:
    """An unbundled grammar is downloaded and dlopen'd, which can crash the process."""
    from tree_sitter_language_pack import get_parser

    from daino.repository.syntax import GRAMMARS

    for extension, grammar in sorted(set(GRAMMARS.items())):
        get_parser(grammar), f"{extension} maps to unbundled grammar {grammar}"


def test_a_failing_grammar_is_only_attempted_once() -> None:
    """An index build touches thousands of files; a broken grammar must not retry."""
    from daino.repository import syntax

    syntax._PARSERS.clear()
    calls: list[str] = []

    def explode(name: str) -> object:
        calls.append(name)
        raise RuntimeError("grammar unavailable")

    original = syntax.get_parser
    syntax.get_parser = explode  # type: ignore[assignment]
    try:
        assert syntax.extract_outline("a.go", b"package main") is None
        assert syntax.extract_outline("b.go", b"package main") is None
        assert syntax.extract_outline("c.go", b"package main") is None
    finally:
        syntax.get_parser = original  # type: ignore[assignment]
        syntax._PARSERS.clear()

    assert calls == ["go"]


def test_terminal_restore_emits_the_sequences_a_crash_skips() -> None:
    """A crashed TUI leaves mouse reporting on; the shell then eats mouse moves."""
    import os
    from unittest.mock import patch

    from daino.utils import crashlog

    read_fd, write_fd = os.pipe()

    class FakeTTY:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return write_fd

    try:
        with (
            patch.object(crashlog.sys, "__stdout__", FakeTTY()),
            patch.object(crashlog.sys, "__stderr__", None),
        ):
            crashlog.restore_terminal()
        os.close(write_fd)
        written = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)

    for sequence in (
        b"\x1b[?1049l",  # leave the alternate screen
        b"\x1b[?25h",  # show the cursor
        b"\x1b[?1003l",  # all-motion mouse reporting off
        b"\x1b[?1006l",  # SGR mouse reporting off
    ):
        assert sequence in written


def test_restore_is_not_wired_to_a_signal_handler() -> None:
    """A Python SIGSEGV handler turns a crash into an endless fault loop.

    Python's C-level handler only sets a flag and returns, so the faulting
    instruction is retried forever. Only faulthandler's own C handler may take
    these signals.
    """
    import signal

    from daino.utils import crashlog

    assert not hasattr(crashlog, "_on_fatal_signal")
    for number in (signal.SIGSEGV, signal.SIGBUS):
        handler = signal.getsignal(number)
        assert not callable(handler) or handler in (signal.SIG_DFL, signal.SIG_IGN)


def test_a_symlink_loop_does_not_take_down_the_process(tmp_path: Path) -> None:
    """A link to an ancestor made the walk infinite and segfaulted pathlib.

    The crash surfaced inside ``Path.relative_to``, which recurses per path
    component: once the walk was deep enough the C stack overflowed and the
    process died with no traceback at all.
    """
    import os

    from daino.repository import RepositoryIndexer

    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    os.symlink(tmp_path, nested / "loop")  # points back at the root
    os.symlink(tmp_path / "a", tmp_path / "sideways")

    index = RepositoryIndexer(tmp_path).build()

    assert [item.path for item in index.files] == ["real.py"]


def test_the_walk_prunes_ignored_directories_instead_of_entering_them(tmp_path: Path) -> None:
    """Descending into node_modules first and filtering after costs a full crawl."""
    from daino.repository import RepositoryIndexer

    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    heavy = tmp_path / "node_modules" / "pkg" / "deep"
    heavy.mkdir(parents=True)
    (heavy / "junk.js").write_text("const a = 1\n", encoding="utf-8")

    walked = list(RepositoryIndexer(tmp_path)._walk())

    assert [str(relative) for _, relative in walked] == ["keep.py"]


def test_the_walk_is_depth_bounded(tmp_path: Path) -> None:
    from daino.repository import RepositoryIndexer
    from daino.repository import indexer as indexer_module

    deep = tmp_path
    for level in range(indexer_module.MAX_INDEX_DEPTH + 5):
        deep = deep / f"L{level}"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "shallow.py").write_text("x = 1\n", encoding="utf-8")

    found = {str(relative) for _, relative in RepositoryIndexer(tmp_path)._walk()}

    assert "shallow.py" in found
    assert not any("buried.py" in name for name in found)


def test_load_does_not_build_the_index(tmp_path: Path) -> None:
    """A view mounting must never trigger a filesystem crawl.

    ``load()`` building on a cache miss is how a startup view ended up indexing
    a whole home directory, which is what crashed.
    """
    from daino.repository import RepositoryIndexer

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    called: list[str] = []
    indexer.build = lambda: called.append("built")  # type: ignore[method-assign]

    index = indexer.load()

    assert called == []
    assert index.files == []
    assert index.root == str(tmp_path.resolve())


# ------------------------------------------ what the budget cost, said out loud


def _scoped_task(*paths: str) -> TaskSpec:
    return TaskSpec(
        id="task",
        title="Rewrite the ledger",
        objective="Rewrite the ledger module",
        expected_files=list(paths),
        acceptance_criteria=["it works"],
        verification_commands=["pytest"],
    )


def test_a_truncated_scoped_file_is_reported_as_truncated(tmp_path: Path) -> None:
    """The agent must be able to tell "part of it" from "all of it"."""
    (tmp_path / "ledger.py").write_text("# ledger\n" + "x = 1\n" * 4_000, encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=900).compile(_scoped_task("ledger.py"))

    assert "ledger.py" in bundle.files
    assert "file truncated" in bundle.files["ledger.py"]
    assert any("part of ledger.py" in note for note in bundle.omitted_context)


def test_a_scoped_file_dropped_for_want_of_budget_is_never_silent(tmp_path: Path) -> None:
    """The bug this exists to fix.

    Under 400 remaining characters a mandatory file is dropped outright, and the
    bundle used to say nothing at all — so a file the task is *scoped to* was
    simply absent, indistinguishable to the agent from one that does not exist
    yet. It would then write the file from scratch over the top of real code.
    """
    (tmp_path / "ledger.py").write_text("x = 1\n" * 4_000, encoding="utf-8")
    (tmp_path / "postings.py").write_text("y = 2\n" * 4_000, encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=600).compile(
        _scoped_task("ledger.py", "postings.py")
    )

    assert "postings.py" not in bundle.files
    assert any("postings.py" in note for note in bundle.omitted_context)
    # And it says why, so the agent reaches for read_file rather than assuming.
    assert any("read_file" in note for note in bundle.omitted_context)


def test_a_bundle_that_fits_reports_nothing_omitted(tmp_path: Path) -> None:
    """A note on every bundle would train the agent to ignore the notes."""
    (tmp_path / "ledger.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=24_000).compile(
        _scoped_task("ledger.py")
    )

    assert bundle.files["ledger.py"].endswith("return 1\n")
    assert bundle.omitted_context == []


def test_related_files_cut_by_the_file_cap_are_counted(tmp_path: Path) -> None:
    """Compact mode packs four files; the agent should know there were more."""
    for index in range(9):
        (tmp_path / f"ledger_{index}.py").write_text(
            f"# ledger part {index}\ndef total_{index}() -> int:\n    return {index}\n",
            encoding="utf-8",
        )
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=24_000, max_files=3).compile(
        _scoped_task()
    )

    assert len(bundle.included_paths) <= 3
    assert any("further related files" in note for note in bundle.omitted_context)


# ------------------------------------------- ranking by distance, not by luck


def _retrieval_task(title: str, objective: str, **kwargs: object) -> TaskSpec:
    return TaskSpec(
        id="task",
        title=title,
        objective=objective,
        acceptance_criteria=["it works"],
        verification_commands=[],
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_task_naming_one_file_pulls_in_its_direct_collaborators() -> None:
    """What the substring matcher could never do.

    The file's caller and its dependencies share no words with the task text;
    nothing but the import edges connects them.
    """
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index(
        {
            "svc/compiler.py": ["svc.store"],
            "svc/builder.py": ["svc.compiler"],
            "svc/store.py": [],
            "unrelated/widget.py": [],
        }
    )
    task = _retrieval_task("Change the compiler", "Change svc/compiler.py")

    ranked = select_candidates(index, task, ["svc/compiler.py"], ImportGraph.build(index))

    assert [item.path for item in ranked[:2]] == ["svc/store.py", "svc/builder.py"]
    assert "unrelated/widget.py" not in [item.path for item in ranked]


def test_a_dependency_behind_a_re_export_outranks_a_word_match() -> None:
    """Without the barrel hop the bundle is one empty __init__ and nothing else.

    `compiler.py` imports `daino.repository`, which is a 239-byte `__init__`
    holding no logic. The file it actually uses, `indexer.py`, is two hops away
    through that re-export.
    """
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index(
        {
            "svc/compiler.py": ["repo"],
            "repo/__init__.py": ["repo.indexer"],
            "repo/indexer.py": [],
            "decoy.py": [],
        }
    )
    # The decoy shares a word with the task; the real dependency does not.
    task = _retrieval_task("Change the compiler", "Change svc/compiler.py and the decoy path")

    ranked = [item.path for item in select_candidates(
        index, task, ["svc/compiler.py"], ImportGraph.build(index)
    )]

    assert ranked.index("repo/indexer.py") < ranked.index("decoy.py")


def test_the_substring_floor_is_exactly_todays_list_in_todays_order() -> None:
    """The invariant: with no graph, the output is what the compiler did before.

    Every candidate set is a superset of the old one, and when nothing resolves
    the ranking degrades to the old list unchanged — so no repository can be made
    worse by this, only better.
    """
    from daino.context.retrieval import lexical_matches, select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    # Go: the outline extractor collects no imports, so there are no edges at all.
    index = repository_index({f"pkg/mod_{index_}.go": [] for index_ in range(6)})
    task = _retrieval_task("Update mod", "Update the mod files in pkg")

    ranked = [item.path for item in select_candidates(index, task, [], ImportGraph.build(index))]

    assert ranked == lexical_matches(index, task)


def test_a_scoped_file_that_does_not_exist_yet_retrieves_its_neighbours() -> None:
    """The ordinary planner case: the task is creating the file.

    It has no edges by definition, so the conventions of the directory it is
    being written into are the only structural evidence there is.
    """
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index({"svc/existing.py": [], "other/far.py": []})
    task = _retrieval_task("Add a module", "Create svc/brand_new.py")

    ranked = [item.path for item in select_candidates(
        index, task, ["svc/brand_new.py"], ImportGraph.build(index)
    )]

    assert ranked[0] == "svc/existing.py"


def test_an_indexed_leaf_does_not_drag_in_its_folder() -> None:
    """Having no edges is information: nothing uses it and it uses nothing."""
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index({"svc/leaf.py": [], "svc/neighbour.py": []})
    task = _retrieval_task("Change the leaf", "Change svc/leaf.py")

    ranked = [item.path for item in select_candidates(
        index, task, ["svc/leaf.py"], ImportGraph.build(index)
    )]

    assert "svc/neighbour.py" not in ranked


def test_a_file_and_its_test_stay_together() -> None:
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index(
        {
            "svc/compiler.py": ["svc.store"],
            "svc/store.py": [],
            "tests/test_store.py": [],
            "tests/test_something_else.py": [],
        }
    )
    task = _retrieval_task("Change the compiler", "Change svc/compiler.py")

    ranked = [item.path for item in select_candidates(
        index, task, ["svc/compiler.py"], ImportGraph.build(index)
    )]

    assert ranked.index("tests/test_store.py") == ranked.index("svc/store.py") + 1
    assert "tests/test_something_else.py" not in ranked


def test_the_ranking_is_deterministic() -> None:
    """The packing that follows is budget-sensitive.

    A wobbling order would make which files the agent sees depend on dictionary
    iteration, so the same task would ground differently on consecutive runs.
    """
    from daino.context.retrieval import select_candidates
    from daino.repository.graph import ImportGraph
    from tests.conftest import repository_index

    index = repository_index({f"pkg/m{i}.py": ["pkg.core"] for i in range(6)} | {"pkg/core.py": []})
    task = _retrieval_task("Change core", "Change pkg/core.py")
    graph = ImportGraph.build(index)

    runs = [
        [item.path for item in select_candidates(index, task, ["pkg/core.py"], graph)]
        for _ in range(5)
    ]

    assert all(run == runs[0] for run in runs)


def test_a_direct_importer_beats_twenty_large_word_matches_at_the_budget(
    tmp_path: Path,
) -> None:
    """The test that would have caught the 38%-of-the-repository behaviour.

    End to end through the real compiler and a real budget: one file the task's
    subject actually imports, against twenty large files that merely share a
    word with it. Ranked by walk order the twenty win and the collaborator is
    never reached, which is exactly what was happening.
    """
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "compiler.py").write_text(
        "from svc.store import load\n\ndef compile_it():\n    return load()\n", encoding="utf-8"
    )
    (tmp_path / "svc" / "store.py").write_text(
        "def load():\n    return 'the collaborator'\n", encoding="utf-8"
    )
    for index_ in range(20):
        (tmp_path / f"compiler_decoy_{index_}.py").write_text(
            f"# compiler decoy {index_}\n" + "filler = 'x' * 80\n" * 60, encoding="utf-8"
        )
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    task = _retrieval_task(
        "Change the compiler", "Change svc/compiler.py", expected_files=["svc/compiler.py"]
    )

    bundle = ContextCompiler(tmp_path, indexer, token_budget=3_000, max_files=3).compile(task)

    assert "svc/compiler.py" in bundle.files
    assert "svc/store.py" in bundle.files, "the file's own dependency lost to twenty decoys"


def test_the_omission_notes_do_not_eat_the_budget_they_describe(tmp_path: Path) -> None:
    """Naming every file that did not fit costs more than it is worth.

    The lexical floor matches a third of a real repository, so listing each miss
    puts thousands of characters of "use read_file" into a bundle whose whole
    purpose is to leave the agent room to work — and trains it to skip the notes.
    """
    for index_ in range(60):
        (tmp_path / f"ledger_{index_}.py").write_text(
            f"# ledger part {index_}\n" + "filler = 'x' * 60\n" * 40, encoding="utf-8"
        )
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=2_000).compile(
        _retrieval_task("Update the ledger", "Update the ledger modules")
    )

    assert len(bundle.omitted_context) <= 6
    assert any("further related files" in note for note in bundle.omitted_context)
    # And the count is honest about how many there were.
    assert any(note.split(" ", 1)[0].isdigit() for note in bundle.omitted_context)


def test_a_near_collaborator_that_did_not_fit_is_named(tmp_path: Path) -> None:
    """A count does not tell the agent which file to reach for."""
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "compiler.py").write_text(
        "from svc.store import load\n\ndef go():\n    return load()\n", encoding="utf-8"
    )
    (tmp_path / "svc" / "store.py").write_text(
        "# store\n" + "value = 'x' * 60\n" * 200, encoding="utf-8"
    )
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()

    bundle = ContextCompiler(tmp_path, indexer, token_budget=400).compile(
        _retrieval_task(
            "Change the compiler", "Change svc/compiler.py", expected_files=["svc/compiler.py"]
        )
    )

    assert "svc/store.py" not in bundle.files
    assert any("svc/store.py" in note for note in bundle.omitted_context)
    assert any("imports or is imported by" in note for note in bundle.omitted_context)
