"""Tree-sitter syntax extraction with deterministic fallbacks in the indexer."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

from daino.schemas import RepositorySymbol

#: Extensions we can parse with a grammar that ships with the language pack.
#:
#: Every entry here must be bundled. ``get_parser`` falls back to downloading a
#: grammar it does not have and loading the result into this process, which
#: means a network call during indexing and a binary of unknown provenance
#: dlopen'd into the interpreter — a mismatched one corrupts the heap and takes
#: the process down with no traceback. ``.cs`` was mapped to ``c_sharp``, which
#: is not bundled under that name and triggered exactly that path; C# now falls
#: back to the regex outline instead.
GRAMMARS = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

DECLARATIONS = {
    "class_declaration": "class",
    "class_definition": "class",
    "function_declaration": "function",
    "function_definition": "function",
    "method_declaration": "method",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "struct_item": "struct",
    "function_item": "function",
    "impl_item": "implementation",
}


@dataclass(frozen=True)
class SyntaxOutline:
    symbols: list[RepositorySymbol]
    parser: str


def _name(node: Node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None and node.type == "impl_item":
        name_node = node.child_by_field_name("type")
    if name_node is None:
        return None
    return source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")


#: Grammar name -> parser, or None once we know a grammar is unusable. Loading a
#: grammar is expensive and a failing one fails every time, so an index build
#: over thousands of files must not retry it per file.
_PARSERS: dict[str, Parser | None] = {}


def _parser_for(grammar: str) -> Parser | None:
    """One parser per grammar, all from the same source.

    Every grammar comes from ``tree_sitter_language_pack`` — including
    TypeScript and TSX, which were previously built directly from
    ``tree_sitter_typescript``. That mixture is what made indexing crash: the
    language pack statically links its own copy of the tree-sitter C runtime, so
    using it alongside a separately-built grammar puts *two* runtimes in the
    process. Trees allocated by one and freed by the other corrupt the heap, and
    the process dies with SIGSEGV or SIGBUS after a hundred-odd files — no
    traceback, no failing file, nothing to grep for.

    One source of grammars is therefore not a style preference but the
    correctness condition. The direct ``tree_sitter_typescript`` import is gone
    for the same reason ``.cs`` was removed from :data:`GRAMMARS`.
    """
    if grammar in _PARSERS:
        return _PARSERS[grammar]
    parser: Parser | None
    try:
        parser = get_parser(grammar)
    except Exception:
        parser = None
    _PARSERS[grammar] = parser
    return parser


def extract_outline(relative: str, source: bytes) -> SyntaxOutline | None:
    """Extract declarations with the grammar matching the file extension."""
    grammar = GRAMMARS.get(Path(relative).suffix.lower())
    if grammar is None:
        return None
    try:
        parser = _parser_for(grammar)
        if parser is None:
            return None
        tree = parser.parse(source)
    except Exception:
        return None
    symbols: list[RepositorySymbol] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = DECLARATIONS.get(node.type)
        if kind:
            name = _name(node, source)
            if name:
                symbols.append(
                    RepositorySymbol(
                        name=name,
                        kind=kind,
                        path=relative,
                        line=node.start_point.row + 1,
                    )
                )
        stack.extend(reversed(node.children))
    return SyntaxOutline(symbols=symbols, parser=f"tree-sitter:{grammar}")


@dataclass(frozen=True, slots=True)
class SyntaxProblem:
    """One place a grammar could not make sense of the source."""

    line: int
    column: int
    #: "error" for a malformed span, "missing" for a token the grammar expected.
    kind: str


#: A file that is broken everywhere produces an error node per token; the first
#: few locate the problem and the rest are noise.
MAX_SYNTAX_PROBLEMS = 10


def syntax_problems(relative: str, source: bytes) -> list[SyntaxProblem] | None:
    """Report where a grammar failed to parse ``source``.

    ``None`` means "no opinion" — there is no bundled grammar for this
    extension, or the parser itself could not be loaded. That is deliberately
    distinct from an empty list, which means the file parsed cleanly: a review
    must never report "syntax OK" for a language it cannot actually read.
    """
    grammar = GRAMMARS.get(Path(relative).suffix.lower())
    if grammar is None:
        return None
    try:
        parser = _parser_for(grammar)
        if parser is None:
            return None
        tree = parser.parse(source)
    except Exception:  # noqa: BLE001 - a parser crash is "no opinion", not a failure
        return None

    problems: list[SyntaxProblem] = []
    stack = [tree.root_node]
    while stack and len(problems) < MAX_SYNTAX_PROBLEMS:
        node = stack.pop()
        # has_error is true for every ancestor of a problem, so only the node
        # that is itself broken is worth reporting.
        if node.type == "ERROR" or node.is_missing:
            problems.append(
                SyntaxProblem(
                    line=node.start_point.row + 1,
                    column=node.start_point.column + 1,
                    kind="missing" if node.is_missing else "error",
                )
            )
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    return problems


# ------------------------------------------------- isolated batch extraction


#: How many times a worker may die during one build before tree-sitter is given
#: up on entirely. A grammar stack that crashes this often is not going to
#: produce a useful index, and restarting it forever would make indexing hang.
MAX_WORKER_RESTARTS = 12
#: How long one file may take. A grammar that hangs is as bad as one that dies.
FILE_TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class BatchOutlines:
    """Outlines for a set of files, and an honest account of what was missed."""

    #: Repository-relative path -> its symbols.
    outlines: dict[str, list[RepositorySymbol]] = field(default_factory=dict)
    #: Files no parser managed. The caller uses its regex fallback for these.
    unparsed: list[str] = field(default_factory=list)
    #: How many times the worker had to be restarted after a native crash.
    restarts: int = 0
    #: Set when tree-sitter was abandoned for this run.
    abandoned: bool = False


def extract_outlines(root: Path, relatives: list[str]) -> BatchOutlines:
    """Parse many files, surviving a native crash in any of them.

    The grammars are third-party native code. On some library combinations they
    corrupt the heap and the process dies with a signal — uncatchable, and fatal
    to whatever was running. So the parsing happens in a child process: a crash
    costs one file, the parent notices the closed pipe, and a fresh worker picks
    up the rest.

    Files the worker could not reach come back in ``unparsed`` rather than as
    silently empty outlines, because "no symbols" and "never parsed" lead to
    different decisions — the caller's regex outline is the right answer for the
    second and the wrong answer for the first.
    """
    import subprocess  # noqa: PLC0415 - only needed on this path

    wanted = [item for item in relatives if GRAMMARS.get(Path(item).suffix.lower())]
    result = BatchOutlines()
    if not wanted:
        return result

    queue = list(wanted)
    argv = [sys.executable, "-m", "daino.repository.syntax_worker", str(root)]
    while queue:
        if result.restarts > MAX_WORKER_RESTARTS:
            # Give up rather than restart forever. Everything left falls back.
            result.unparsed.extend(queue)
            result.abandoned = True
            return result
        try:
            worker = subprocess.Popen(  # noqa: S603
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(root),
                text=True,
                bufsize=1,
            )
        except OSError:
            result.unparsed.extend(queue)
            result.abandoned = True
            return result
        try:
            while queue:
                relative = queue[0]
                assert worker.stdin is not None
                assert worker.stdout is not None
                try:
                    worker.stdin.write(relative + "\n")
                    worker.stdin.flush()
                    line = worker.stdout.readline()
                except (BrokenPipeError, OSError, ValueError):
                    line = ""
                if not line:
                    # The worker died on this file. Record it, skip it, restart.
                    queue.pop(0)
                    result.unparsed.append(relative)
                    result.restarts += 1
                    break
                queue.pop(0)
                try:
                    payload = json.loads(line)
                except ValueError:
                    result.unparsed.append(relative)
                    continue
                if payload.get("error") or not payload.get("parser"):
                    result.unparsed.append(relative)
                    continue
                result.outlines[relative] = [
                    RepositorySymbol(
                        name=str(item.get("name", "")),
                        kind=str(item.get("kind", "symbol")),
                        path=str(item.get("path", relative)),
                        line=int(item.get("line", 1)),
                    )
                    for item in payload.get("symbols", [])
                ]
        finally:
            _stop(worker)
    return result


def _stop(worker: object) -> None:
    """Close a worker down without letting its teardown raise."""
    import contextlib  # noqa: PLC0415

    stdin = getattr(worker, "stdin", None)
    if stdin is not None:
        with contextlib.suppress(Exception):
            stdin.close()
    with contextlib.suppress(Exception):
        worker.wait(timeout=5)  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        if worker.poll() is None:  # type: ignore[attr-defined]
            worker.kill()  # type: ignore[attr-defined]
