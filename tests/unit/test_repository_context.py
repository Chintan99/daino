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
