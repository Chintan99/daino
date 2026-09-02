"""Mechanical review of a change, before any model looks at it.

Everything here is deterministic and reads only what the change introduced.
Two reasons that matters. A reviewer that reports pre-existing problems in a
file someone merely touched is a reviewer people learn to skip; and a model
asked to notice a stray ``console.log`` or a conflict marker will sometimes
miss it, whereas a regex never will. The model's attention is worth spending on
what the change *means*, so the mechanical layer clears the ground first.

Findings reuse :class:`~daino.schemas.QAFinding` so a review, a repository scan,
and a live probe all produce one comparable severity vocabulary.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from daino.repository.syntax import syntax_problems
from daino.review.diffs import FileChange, Line
from daino.schemas import QAFinding, QAFindingCategory, QASeverity
from daino.security import audit

#: A change past this is not reviewed carefully by anyone, human or otherwise.
LARGE_CHANGE_LINES = 800
#: A single file past this is usually generated, vendored, or wants splitting.
LARGE_FILE_LINES = 600
#: Long enough that a diff of it is unreadable side by side.
LONG_LINE_CHARS = 200
#: Beyond this many top-level areas, a change is doing several things at once.
MIXED_CONCERN_AREAS = 5

#: Paths whose changes are tests rather than the behaviour under test.
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e)(/|$)|(^|/)(test_[^/]+|[^/]+_test|[^/]+\.(test|spec))\.",
    re.IGNORECASE,
)

#: Files whose change usually has to be accompanied by something else.
_MANIFESTS: dict[str, tuple[str, ...]] = {
    "package.json": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock"),
    "pyproject.toml": ("uv.lock", "poetry.lock", "requirements.txt"),
    "Cargo.toml": ("Cargo.lock",),
    "go.mod": ("go.sum",),
    "composer.json": ("composer.lock",),
}

#: Text that means someone was debugging and did not take it back out. Only
#: constructs that are never deliberate in committed code: a bare ``print`` or
#: ``fmt.Println`` is how a command-line tool talks to its user, and flagging
#: those would make the whole check noise.
_DEBUG = (
    (re.compile(r"(?<![\w.])console\.(log|debug|dir|trace)\s*\("), "console logging"),
    (re.compile(r"(?<![\w.])debugger\s*;?\s*$"), "a debugger statement"),
    (
        re.compile(r"(?<![\w.])(pdb|ipdb|pudb)\.set_trace\s*\(|(?<![\w.])breakpoint\s*\(\s*\)"),
        "a breakpoint",
    ),
    (re.compile(r"(?<![\w.])binding\.pry\b"), "a pry breakpoint"),
    (re.compile(r"(?<![\w.])dbg!\s*\("), "a dbg! macro"),
    (
        re.compile(r"(?i)\b(fdescribe|fit|describe\.only|it\.only|test\.only)\s*\("),
        "a focused test",
    ),
    (
        re.compile(r"(?i)\b(xdescribe|xit|describe\.skip|it\.skip|test\.skip)\s*\("),
        "a skipped test",
    ),
)

_CONFLICT = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")

#: A marker is a standalone word. The hyphen exclusions matter: an identifier
#: like ``review-todo-added`` contains the word but is not a marker.
_MARKER = re.compile(r"(?<![-\w])(TODO|FIXME|HACK|XXX)(?![-\w])[:( ]?", re.IGNORECASE)

#: Characters that render as nothing, or as something other than what they are.
#: Bidirectional overrides are the Trojan Source attack: source that reads one
#: way to a human and compiles another way.
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩"
_INVISIBLE = "​‌‍⁠﻿­"

#: Python's own parser is stricter and better-located than any grammar, and it
#: is always available. Other formats have a parser in the standard library.
_PYTHON = {".py", ".pyi"}


@dataclass(frozen=True, slots=True)
class ChangeStats:
    """The shape of a change, for the summary line and the size gate."""

    files: int
    insertions: int
    deletions: int
    tests_touched: bool
    areas: tuple[str, ...]


def review_change(
    changes: list[FileChange],
    *,
    read_file: object = None,
) -> list[QAFinding]:
    """Every mechanical finding for one change.

    ``read_file`` is an optional ``(path) -> str | None`` used for the checks
    that need the file as it now stands rather than only its diff — syntax
    validity is the main one, because a patch fragment never parses on its own.
    """
    findings: list[QAFinding] = []
    for change in changes:
        findings.extend(_per_file(change, read_file))
    findings.extend(_cross_file(changes))
    return audit.cap_per_rule(audit.deduplicate(findings))


def summarise(changes: list[FileChange]) -> ChangeStats:
    areas: list[str] = []
    for change in changes:
        head = Path(change.path).parts
        areas.append(head[0] if len(head) > 1 else ".")
    return ChangeStats(
        files=len(changes),
        insertions=sum(item.insertions for item in changes),
        deletions=sum(item.deletions for item in changes),
        tests_touched=any(TEST_PATH.search(item.path) for item in changes),
        areas=tuple(dict.fromkeys(areas)),
    )


# ------------------------------------------------------------------ per file


def _prose_lines(change: FileChange, read_file: object) -> frozenset[int]:
    """Line numbers inside a multi-line Python string.

    Docstrings and prose blocks routinely contain the exact text a code rule
    looks for — this module's own docstring says ``shell=True`` while
    explaining the rule that finds it. Those lines are documentation, not code.

    Only multi-line strings, and only for the checks that describe code. The
    checks whose whole point is that they hide inside prose — conflict markers
    and deceptive characters — still read every line.
    """
    if change.suffix not in _PYTHON or not callable(read_file):
        return frozenset()
    content = read_file(change.path)
    if not isinstance(content, str):
        return frozenset()
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return frozenset()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.end_lineno is not None
            and node.end_lineno > node.lineno
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return frozenset(lines)


def _per_file(change: FileChange, read_file: object) -> Iterator[QAFinding]:
    if change.kind == "deleted":
        return
    if change.binary:
        yield _finding(
            "review-binary-file",
            f"Binary file {'added' if change.kind == 'added' else 'changed'}",
            "low",
            change.path,
            detail="A binary cannot be reviewed as a diff; confirm it belongs in the repository.",
            remediation=(
                "Prefer a generated artifact or an external store over a committed binary."
            ),
            category="quality",
        )
        return

    prose = _prose_lines(change, read_file)
    yield from _syntax(change, read_file)
    yield from _added_line_checks(change, prose)
    yield from _shape(change)
    yield from _weakened_tests(change)


def _syntax(change: FileChange, read_file: object) -> Iterator[QAFinding]:
    """Does the file still parse?

    The single most valuable mechanical check, because a file that does not
    parse fails everything downstream and a diff can hide it: the broken line
    may be context the patch never shows.
    """
    if not callable(read_file):
        return
    content = read_file(change.path)
    if not isinstance(content, str):
        return
    suffix = change.suffix

    if suffix in _PYTHON:
        try:
            ast.parse(content)
        except SyntaxError as exc:
            yield _finding(
                "review-syntax",
                f"{change.path} does not parse: {exc.msg}",
                "critical",
                change.path,
                line=exc.lineno,
                detail=f"Python syntax error at line {exc.lineno}, column {exc.offset}.",
                remediation="Fix the syntax error; nothing downstream can run until it parses.",
            )
        return

    structured = {
        ".json": _parse_json,
        ".yaml": _parse_yaml,
        ".yml": _parse_yaml,
        ".toml": _parse_toml,
    }.get(suffix)
    if structured is not None:
        problem = structured(content)
        if problem:
            yield _finding(
                "review-syntax",
                f"{change.path} is not valid {suffix.lstrip('.').upper()}",
                "critical",
                change.path,
                detail=problem,
                remediation="Fix the document so it parses.",
            )
        return

    for parsed in syntax_problems(change.path, content.encode("utf-8", "replace")) or []:
        yield _finding(
            "review-syntax",
            f"{change.path} does not parse cleanly at line {parsed.line}",
            "high",
            change.path,
            line=parsed.line,
            detail=(
                f"The grammar reported a {parsed.kind} node at line {parsed.line}, "
                f"column {parsed.column}."
            ),
            remediation="Check the syntax around that line.",
        )


def _added_line_checks(change: FileChange, prose: frozenset[int]) -> Iterator[QAFinding]:
    """Everything that reads one introduced line at a time.

    Two filters apply throughout, for the same reason the repository audit
    applies them: a line that *defines* a pattern is not a line that does the
    thing, and a test that asserts a pattern is caught has to contain it. Both
    are kept visible but neither can block a merge on its own.
    """
    fixture = audit.is_non_production(change.path)
    for line in change.added:
        text = line.text
        stripped_line = text.strip()
        if audit.is_non_executable(stripped_line, change.path):
            continue

        if _CONFLICT.match(text):
            yield _finding(
                "review-conflict-marker",
                "Merge conflict marker left in the file",
                _severity("critical", fixture),
                change.path,
                line=line.number,
                detail=text.strip()[:120],
                remediation="Finish the merge and remove the marker.",
            )
            continue

        stripped = text.strip()
        if not _is_comment(stripped) and line.number not in prose:
            for pattern, what in _DEBUG:
                if pattern.search(text):
                    yield _finding(
                        "review-debug-leftover",
                        f"Change introduces {what}",
                        _severity("medium" if "test" in what else "low", fixture),
                        change.path,
                        line=line.number,
                        detail=stripped[:160],
                        remediation="Remove it, or replace it with the project's logger.",
                        category="quality",
                    )
                    break

        marker = _MARKER.search(text)
        if marker is not None and line.number not in prose:
            yield _finding(
                "review-todo-added",
                f"Change adds a {marker.group(1).upper()}",
                "info",
                change.path,
                line=line.number,
                detail=stripped[:160],
                remediation="Resolve it, or link it to an issue so it is not lost.",
                category="quality",
            )

        yield from _unicode(change, line, fixture)

        if len(text) > LONG_LINE_CHARS and not _is_data(change.suffix):
            yield _finding(
                "review-long-line",
                f"Line is {len(text)} characters",
                "info",
                change.path,
                line=line.number,
                detail="Long lines make a side-by-side diff unreadable.",
                remediation="Wrap it, or let the formatter.",
                category="quality",
            )

        if text.rstrip() != text and text.strip():
            yield _finding(
                "review-trailing-whitespace",
                "Trailing whitespace on an added line",
                "info",
                change.path,
                line=line.number,
                detail="Adds diff noise on every later edit to the line.",
                remediation="Strip it, or enable the formatter on save.",
                category="quality",
            )

    # Secrets and insecure patterns, scanned over only what was introduced.
    yield from _reuse_repository_rules(change, prose)


def _unicode(change: FileChange, line: Line, fixture: bool) -> Iterator[QAFinding]:
    """Text that does not render as what it is.

    Bidirectional overrides let source read one way to a reviewer and compile
    another way. It is rare and it is worth never missing.
    """
    if any(char in line.text for char in _BIDI):
        yield _finding(
            "review-bidi-control",
            "Bidirectional control character in source",
            _severity("critical", fixture),
            change.path,
            line=line.number,
            detail=(
                "This makes the line display differently from how it is compiled, "
                "which is how source is made to hide what it does."
            ),
            remediation="Remove the control character unless the file genuinely needs bidi text.",
            cwe="CWE-451",
            confidence="low" if fixture else "high",
        )
    elif any(char in line.text for char in _INVISIBLE):
        yield _finding(
            "review-invisible-character",
            "Zero-width or invisible character in source",
            _severity("low", fixture),
            change.path,
            line=line.number,
            detail="Invisible characters can hide differences between look-alike identifiers.",
            remediation="Remove it unless it is deliberate.",
            cwe="CWE-451",
            confidence="low" if fixture else "high",
        )


def _reuse_repository_rules(change: FileChange, prose: frozenset[int]) -> Iterator[QAFinding]:
    """Run the repository audit's own rules over the introduced lines only.

    One rule table for "this code is unsafe", whether it is found by a full
    scan or by a review. The line numbers are remapped back onto the new file
    so a finding points where the reviewer is looking.
    """
    if not change.added:
        return
    numbers = [line.number for line in change.added]
    text = change.added_text()
    # A credential in a docstring is still a credential, so secrets read
    # everything; a code pattern quoted in prose is not code, so those lines
    # are blanked while keeping the numbering intact.
    code = "\n".join("" if line.number in prose else line.text for line in change.added)
    for finding in [
        *audit.scan_secrets(change.path, text),
        *audit.scan_patterns(change.path, code),
    ]:
        index = (finding.line or 1) - 1
        real = numbers[index] if 0 <= index < len(numbers) else finding.line
        yield finding.model_copy(
            update={
                "id": f"{finding.reference}:{change.path}:{real}",
                "line": real,
                "source": "change review",
            }
        )


def _shape(change: FileChange) -> Iterator[QAFinding]:
    if change.insertions > LARGE_FILE_LINES:
        yield _finding(
            "review-large-file-change",
            f"{change.insertions} lines added to one file",
            "low",
            change.path,
            detail="A change this size in one file is rarely reviewed line by line.",
            remediation="Split it, or say in the description which part needs real attention.",
            category="quality",
        )


def _weakened_tests(change: FileChange) -> Iterator[QAFinding]:
    """Tests that lost assertions.

    The failure mode this catches is real and hard to see in a large diff: a
    change that makes a test pass by asking it less.
    """
    if not TEST_PATH.search(change.path) or change.kind == "added":
        return
    removed = sum(1 for line in change.removed if _is_assertion(line.text))
    added = sum(1 for line in change.added if _is_assertion(line.text))
    if removed > added:
        yield _finding(
            "review-weakened-test",
            f"{change.path} removes {removed - added} more assertion(s) than it adds",
            "high",
            change.path,
            detail=(
                "A change that reduces what a test checks can turn a real failure green. "
                "Confirm the assertions were replaced rather than dropped."
            ),
            remediation="Keep the coverage, or say in the change why it is no longer needed.",
            category="tests",
        )


# ---------------------------------------------------------------- cross file


def _cross_file(changes: list[FileChange]) -> Iterator[QAFinding]:
    stats = summarise(changes)
    paths = {item.path for item in changes}
    live = [item for item in changes if item.kind != "deleted"]

    if stats.insertions + stats.deletions > LARGE_CHANGE_LINES:
        yield _finding(
            "review-large-change",
            f"{stats.insertions + stats.deletions} lines across {stats.files} files",
            "low",
            "",
            detail="Review quality falls off sharply past a few hundred lines.",
            remediation="Split it into reviewable steps where the history allows.",
            category="quality",
        )

    if len(stats.areas) > MIXED_CONCERN_AREAS:
        yield _finding(
            "review-mixed-concerns",
            f"Change touches {len(stats.areas)} top-level areas",
            "info",
            "",
            detail="Areas: " + ", ".join(stats.areas[:8]),
            remediation="Separate unrelated work so each part can be judged on its own.",
            category="quality",
        )

    behaviour = [
        item
        for item in live
        if not TEST_PATH.search(item.path) and _is_source(item.suffix) and item.insertions
    ]
    if behaviour and not stats.tests_touched:
        yield _finding(
            "review-no-tests",
            "Source changed with no test touched",
            "medium",
            "",
            detail="Changed without tests: " + ", ".join(item.path for item in behaviour[:6]),
            remediation="Add or update a test, or say why the change is not testable.",
            category="tests",
        )

    for manifest, lockfiles in _MANIFESTS.items():
        if not any(item.path == manifest or item.path.endswith(f"/{manifest}") for item in live):
            continue
        if any(any(path.endswith(lock) for lock in lockfiles) for path in paths):
            continue
        yield _finding(
            "review-lockfile-drift",
            f"{manifest} changed without its lockfile",
            "medium",
            manifest,
            detail=f"Expected one of: {', '.join(lockfiles)}.",
            remediation="Regenerate the lockfile so installs are reproducible.",
            category="dependencies",
        )

    yield from _migration_gap(live, paths)
    yield from _removed_symbols(changes)


def _migration_gap(live: list[FileChange], paths: set[str]) -> Iterator[QAFinding]:
    models = [
        item.path
        for item in live
        if _MODEL_FILE.search(item.path) and not _MIGRATION_DIR.search(item.path)
    ]
    if not models:
        return
    if any(_MIGRATION_DIR.search(path) for path in paths):
        return
    yield _finding(
        "review-migration-gap",
        "Persistence models changed with no migration",
        "medium",
        models[0],
        detail="Changed: " + ", ".join(models[:4]),
        remediation="Add a migration, or confirm the change needs no schema update.",
        category="quality",
    )


def _removed_symbols(changes: list[FileChange]) -> Iterator[QAFinding]:
    """Public definitions the change deletes.

    Approximate on purpose — it reads removed lines, not the two file versions
    — so it is reported as something to confirm rather than as a breakage.
    """
    for change in changes:
        if change.suffix not in _PYTHON or change.kind == "added":
            continue
        removed = _definitions(change.removed)
        added = _definitions(change.added)
        gone = sorted(removed - added)
        if not gone:
            continue
        yield _finding(
            "review-removed-symbol",
            f"{change.path} removes {len(gone)} public definition(s)",
            "medium",
            change.path,
            detail="Removed: " + ", ".join(gone[:8]),
            remediation="Confirm nothing outside this change imports them.",
            category="quality",
        )


#: A file whose change usually implies a schema change, and where a migration
#: would live if one were written.
_MODEL_FILE = re.compile(r"(^|/)(models?|entities|schema)\.py$")
_MIGRATION_DIR = re.compile(r"(^|/)(migrations?|alembic/versions)/")

_DEFINITION = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def _definitions(lines: Iterable[Line]) -> set[str]:
    found: set[str] = set()
    for line in lines:
        if line.text[:1] in {" ", "\t"}:
            # Indented, so it is a method or a nested helper rather than the
            # module's own surface.
            continue
        match = _DEFINITION.match(line.text.strip())
        if match and not match.group(1).startswith("_"):
            found.add(match.group(1))
    return found


# ---------------------------------------------------------------- utilities


def _parse_json(content: str) -> str:
    try:
        json.loads(content)
    except ValueError as exc:
        return str(exc)
    return ""


def _parse_yaml(content: str) -> str:
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return str(exc).splitlines()[0] if str(exc) else "invalid YAML"
    return ""


def _parse_toml(content: str) -> str:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        return str(exc)
    return ""


_ASSERTION = re.compile(
    r"(?<![\w.])(assert\b|self\.assert\w+\s*\(|expect\s*\(|should\b|require\.\w+\s*\(|"
    r"pytest\.raises|assertThat\s*\()"
)


def _is_assertion(text: str) -> bool:
    stripped = text.strip()
    return bool(_ASSERTION.search(stripped)) and not _is_comment(stripped)


def _severity(base: QASeverity, fixture: bool) -> QASeverity:
    """A finding in a fixture is real but is never what blocks a merge."""
    return audit.demote(base) if fixture else base


def _is_comment(stripped: str) -> bool:
    return stripped.startswith(("#", "//", "*", "/*", "<!--"))


_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
    }
)
_DATA_SUFFIXES = frozenset(
    {".csv", ".json", ".lock", ".map", ".md", ".svg", ".tsv", ".yaml", ".yml"}
)


def _is_source(suffix: str) -> bool:
    return suffix in _SOURCE_SUFFIXES


def _is_data(suffix: str) -> bool:
    return suffix in _DATA_SUFFIXES


def _finding(
    reference: str,
    title: str,
    severity: QASeverity,
    path: str,
    *,
    line: int | None = None,
    detail: str = "",
    remediation: str = "",
    cwe: str = "",
    category: QAFindingCategory = "vulnerability",
    confidence: Literal["high", "medium", "low"] = "high",
) -> QAFinding:
    return QAFinding(
        id=f"{reference}:{path}:{line or 0}",
        title=title,
        severity=severity,
        category=category,
        source="change review",
        location=path,
        line=line,
        detail=detail,
        remediation=remediation,
        cwe=cwe,
        reference=reference,
        confidence=confidence,
    )
