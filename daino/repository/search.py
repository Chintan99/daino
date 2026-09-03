"""Searching and replacing across the repository.

The distinction that makes this worth its own module: **a replace is previewed
before it is applied**. Search-and-replace over a whole tree is one of the few
editor operations that can silently ruin a working copy, and the only defence
that works is showing every line that would change and requiring a second act to
change them.

Three design notes:

* **Filters are globs, not another regex.** ``include``/``exclude`` take
  ``src/**/*.ts``-shaped patterns because that is what people already know from
  every other search box, and mixing two pattern languages in one form is how a
  filter silently matches nothing.
* **Replacement happens per file, in one write.** Line-by-line writes on a large
  file are slow and leave a half-replaced file behind if anything fails; a single
  read-modify-write either succeeds or does not.
* **Binary and oversized files are skipped, and counted.** A search that quietly
  ignored half a repository would be worse than one that admitted it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from daino.config import paths

#: Directories never worth searching. Generated output and dependency trees
#: swamp any result list they appear in.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "coverage",
        ".idea",
        ".vscode",
    }
)

#: A file bigger than this is not something anyone is reading search results
#: from — it is generated, minified, or data.
MAX_FILE_BYTES = 2_000_000
#: How much of a file is inspected for NUL bytes before deciding it is binary.
#: The same heuristic and the same window Git uses, which matters because the
#: two should agree about what counts as a text file.
BINARY_SNIFF_BYTES = 8_000
#: Hard ceiling on results, so a one-character query cannot exhaust memory.
MAX_MATCHES = 5_000


@dataclass(slots=True)
class Match:
    path: str
    line: int
    #: One-based column of the match start, so the editor can select it.
    column: int
    #: Length of the matched text, for the same reason.
    length: int
    #: The whole line, untrimmed — leading whitespace is context.
    text: str
    #: What this line would become. Empty for a plain search.
    replacement: str = ""


@dataclass(slots=True)
class SearchQuery:
    query: str
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    #: Glob patterns a path must match at least one of. Empty means all.
    include: tuple[str, ...] = ()
    #: Glob patterns that exclude a path outright.
    exclude: tuple[str, ...] = ()
    limit: int = 500

    def pattern(self) -> re.Pattern[str]:
        """The compiled matcher for this query.

        A literal query is escaped rather than passed through, so searching for
        ``a.b`` finds ``a.b`` and not ``axb`` — the single most common surprise
        in a search box that treats everything as a regex.
        """
        source = self.query if self.regex else re.escape(self.query)
        if self.whole_word:
            source = rf"\b(?:{source})\b"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        return re.compile(source, flags)


@dataclass(slots=True)
class SearchResult:
    matches: list[Match] = field(default_factory=list)
    #: Files that held at least one match.
    files: int = 0
    #: True when the limit cut the results short, so the UI can say so rather
    #: than implying it found everything.
    truncated: bool = False
    #: Files skipped for being binary or oversized. Counted, never hidden.
    skipped: int = 0
    #: Set when the query itself is invalid — a malformed regex, most often.
    error: str = ""


def read_text(path: Path) -> str | None:
    """A file's text, or None when it should not be searched.

    The NUL check is why this exists rather than a bare ``read_text``: a NUL
    byte is perfectly valid UTF-8, so a decode that succeeds is not evidence of
    a text file. Without it, searching a compiled binary or a database happily
    "succeeds" and reports lines of mojibake.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        with path.open("rb") as handle:
            head = handle.read(BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None
            rest = handle.read()
        return (head + rest).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or (name.startswith(".") and name not in {".github"})


def iter_files(root: Path) -> list[Path]:
    """Every file worth searching, depth-first and sorted for stable output.

    Prunes whole directories rather than filtering their files one by one:
    walking into ``node_modules`` to reject each of its 40,000 files is the
    difference between a search that feels instant and one that does not.
    """
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                # Following symlinks risks walking a cycle, or out of the repo.
                continue
            if entry.is_dir():
                relative = entry.relative_to(root)
                # Workspace documents live under Daino's own state directory
                # but are the user's writing, so searching has to reach them.
                if paths.in_workspaces(relative) or not _skip_dir(entry.name):
                    stack.append(entry)
                continue
            if entry.is_file():
                found.append(entry)
    found.sort()
    return found


def matches_filters(relative: str, query: SearchQuery) -> bool:
    """Whether a path passes the include/exclude globs.

    A pattern with no slash matches on the basename too, so ``*.ts`` behaves the
    way people expect rather than only matching files at the repository root.
    """
    name = PurePosixPath(relative).name
    if query.exclude and any(
        fnmatch(relative, pattern) or fnmatch(name, pattern) for pattern in query.exclude
    ):
        return False
    if not query.include:
        return True
    return any(fnmatch(relative, pattern) or fnmatch(name, pattern) for pattern in query.include)


def search(root: Path, query: SearchQuery, *, replacement: str | None = None) -> SearchResult:
    """Find every match, optionally computing what a replacement would produce.

    ``replacement`` makes this a *preview*: each match carries the line it would
    become, and nothing is written. Applying is :func:`apply_replacement`, which
    takes the same query again — so what gets written is always recomputed from
    the same rule the user saw, never from a stale diff.
    """
    result = SearchResult()
    if not query.query:
        return result
    try:
        pattern = query.pattern()
    except re.error as exc:
        result.error = f"Invalid pattern: {exc}"
        return result

    limit = min(query.limit, MAX_MATCHES)
    for path in iter_files(root):
        relative = path.relative_to(root).as_posix()
        if not matches_filters(relative, query):
            continue
        text = read_text(path)
        if text is None:
            result.skipped += 1
            continue

        found_here = False
        for number, line in enumerate(text.splitlines(), start=1):
            for hit in pattern.finditer(line):
                found_here = True
                result.matches.append(
                    Match(
                        path=relative,
                        line=number,
                        column=hit.start() + 1,
                        length=max(1, hit.end() - hit.start()),
                        text=line,
                        replacement=(
                            _substitute(pattern, line, replacement, query)
                            if replacement is not None
                            else ""
                        ),
                    )
                )
                if len(result.matches) >= limit:
                    result.truncated = True
                    break
            if result.truncated:
                break
        if found_here:
            result.files += 1
        if result.truncated:
            break
    return result


def _substitute(pattern: re.Pattern[str], line: str, replacement: str, query: SearchQuery) -> str:
    """What one line becomes.

    Backreferences (``\\1``, ``\\g<name>``) work only for a regex search. In a
    literal search the replacement is taken literally too, because someone
    replacing ``C:\\path`` with ``D:\\path`` did not mean to write an escape.
    """
    if query.regex:
        try:
            return pattern.sub(replacement, line)
        except re.error:
            return line
    return pattern.sub(lambda _: replacement, line)


@dataclass(slots=True)
class ReplacementSummary:
    files: list[str] = field(default_factory=list)
    replacements: int = 0
    errors: list[str] = field(default_factory=list)


def apply_replacement(
    root: Path,
    query: SearchQuery,
    replacement: str,
    *,
    only_paths: list[str] | None = None,
) -> ReplacementSummary:
    """Write the replacement to disk.

    ``only_paths`` narrows it to files the user actually ticked, which is what
    makes a preview meaningful — being shown 200 matches and then having no way
    to accept 190 of them would make the preview a formality.

    Recomputed from the query rather than applied from the preview's line text:
    a file that changed between preview and apply must not be written from a
    stale snapshot.
    """
    summary = ReplacementSummary()
    try:
        pattern = query.pattern()
    except re.error as exc:
        summary.errors.append(f"Invalid pattern: {exc}")
        return summary

    wanted = set(only_paths) if only_paths else None
    for path in iter_files(root):
        relative = path.relative_to(root).as_posix()
        if wanted is not None and relative not in wanted:
            continue
        if wanted is None and not matches_filters(relative, query):
            continue
        text = read_text(path)
        if text is None or not pattern.search(text):
            continue
        replaced = "\n".join(
            _substitute(pattern, line, replacement, query) for line in text.splitlines()
        )
        # splitlines drops the trailing newline; putting it back is the
        # difference between a clean diff and one that touches the last line of
        # every file it edits.
        if text.endswith("\n"):
            replaced += "\n"
        count = len(pattern.findall(text))
        try:
            path.write_text(replaced, encoding="utf-8")
        except OSError as exc:
            summary.errors.append(f"{relative}: {exc}")
            continue
        summary.files.append(relative)
        summary.replacements += count
    return summary
