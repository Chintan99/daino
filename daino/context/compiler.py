"""Budgeted, task-specific context compilation."""

from __future__ import annotations

from pathlib import Path

from daino.context.retrieval import select_candidates
from daino.repository import RepositoryIndexer
from daino.repository.graph import ImportGraph, is_test_path
from daino.schemas import ContextBundle, TaskSpec

#: How many near collaborators are named individually before the rest become a
#: count. Enough to be useful, few enough that the notes stay readable.
_NAMED_NEAR_MISSES = 5


class ContextCompiler:
    """Selects exact relevant code while keeping prompts below a hard budget."""

    def __init__(
        self,
        root: Path,
        indexer: RepositoryIndexer,
        token_budget: int = 24_000,
        *,
        max_files: int | None = None,
        per_file_tokens: int | None = None,
        prefer_symbol_slices: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.indexer = indexer
        self.token_budget = token_budget
        self.max_files = max_files
        self.per_file_tokens = per_file_tokens
        self.prefer_symbol_slices = prefer_symbol_slices

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def compile(
        self,
        task: TaskSpec,
        *,
        decisions: list[str] | None = None,
        failure_summary: str | None = None,
    ) -> ContextBundle:
        index = self.indexer.load()
        # Files the task is scoped to must be present: editing one the agent
        # cannot see means guessing at its contents and rewriting it blind.
        required = list(dict.fromkeys([*task.expected_files, *task.allowed_files]))
        indexed_files = {item.path: item for item in index.files}
        # Ranked by import distance from what the task actually names, with the
        # old substring match kept as the floor. It used to be the *only* signal:
        # on this repository it matched 187 of 506 files, and the bundle was then
        # filled in filesystem walk order — so a compact profile's four slots
        # went to whatever happened to sort first, and the file's own caller was
        # never reached. `self.indexer.tests()` is gone with it: it re-loaded and
        # re-parsed the whole index, a second time, on every compile.
        candidates = select_candidates(index, task, required, ImportGraph.build(index))
        discovered = [item.path for item in candidates]
        near_misses = {item.path for item in candidates if item.near}

        files: dict[str, str] = {}
        tests: dict[str, str] = {}
        # What the budget cost the agent. Without this a scoped file could be
        # truncated — or, under 400 remaining characters, dropped outright — and
        # nothing in the bundle said so: the agent saw a file it was told to
        # edit simply missing, and had no way to tell that from a file that does
        # not exist yet. It has read_file; naming the near miss is what lets it
        # use it.
        omitted: list[str] = []
        #: Related files the budget could not take. Counted rather than listed,
        #: except for the nearest few: on this repository the lexical floor
        #: matches 187 files, and naming every one that did not fit would put
        #: 3,000 characters of "use read_file" into a bundle whose whole purpose
        #: is to leave the agent room to work.
        overflowed: list[str] = []
        used = self._estimate_tokens(task.model_dump_json())

        def include(relative: str, *, mandatory: bool) -> None:
            nonlocal used
            path = (self.root / relative).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return
            if relative in files or relative in tests:
                return
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return
            if self.prefer_symbol_slices and self.per_file_tokens:
                content = _focused_source(
                    content,
                    indexed_files.get(relative),
                    task,
                    self.per_file_tokens,
                )
            cost = self._estimate_tokens(content)
            if used + cost > self.token_budget:
                if not mandatory:
                    overflowed.append(relative)
                    return
                # Truncate rather than omit, and say so, so the agent knows it is
                # looking at part of the file instead of assuming it has all of it.
                remaining = max(0, self.token_budget - used) * 4
                if remaining < 400:
                    omitted.append(f"{relative} (in task scope, no budget left); use read_file")
                    return
                content = content[:remaining] + "\n… file truncated to fit the context budget\n"
                cost = self._estimate_tokens(content)
                omitted.append(f"part of {relative}; use read_file with offset/limit")
            target = tests if is_test_path(relative) else files
            target[relative] = content
            used += cost

        for relative in required:
            include(relative, mandatory=True)
        capped = 0
        for relative in dict.fromkeys(discovered):
            if self.max_files is not None and len(files) + len(tests) >= self.max_files:
                remaining_paths = dict.fromkeys(discovered)
                capped = sum(
                    1 for item in remaining_paths if item not in files and item not in tests
                )
                break
            include(relative, mandatory=False)
        # A direct collaborator that did not fit is worth naming individually:
        # the agent has read_file, and what it lacks is any way to know the file
        # exists. A distant word match is not, so it is only counted.
        missed_near = [
            path
            for path in discovered
            if path in near_misses and path not in files and path not in tests
        ]
        omitted.extend(
            f"{path} (imports or is imported by this task's files); use read_file"
            for path in missed_near[:_NAMED_NEAR_MISSES]
        )
        remaining = capped + len(overflowed) + max(0, len(missed_near) - _NAMED_NEAR_MISSES)
        if remaining:
            omitted.append(f"{remaining} further related files; use read_file/grep")
        return ContextBundle(
            task=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            architecture_decisions=decisions or [],
            files=files,
            tests=tests,
            failure_summary=failure_summary,
            token_estimate=used,
            included_paths=[*files, *tests],
            omitted_context=list(dict.fromkeys(omitted)),
        )


def _focused_source(content: str, indexed: object, task: TaskSpec, token_limit: int) -> str:
    """Return exact symbol windows for a large file without rewriting source lines."""
    if len(content) <= token_limit * 4:
        return content
    symbols = list(getattr(indexed, "symbols", []) or [])
    wanted = {item.casefold() for item in task.relevant_symbols}
    objective_terms = {
        term.strip("()[]{}.,:`'").casefold()
        for term in f"{task.title} {task.objective}".split()
        if len(term.strip("()[]{}.,:`'")) > 3
    }
    matches = [
        item
        for item in symbols
        if str(getattr(item, "name", "")).casefold() in wanted
        or any(term in str(getattr(item, "name", "")).casefold() for term in objective_terms)
    ]
    if not matches:
        return content
    lines = content.splitlines()
    windows: list[tuple[int, int]] = []
    for symbol in matches[:4]:
        line = max(1, int(getattr(symbol, "line", 1)))
        start = max(1, line - 30)
        end = min(len(lines), line + 90)
        if windows and start <= windows[-1][1] + 10:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    rendered: list[str] = [
        "… focused source excerpts; use read_file with offset/limit for omitted regions …"
    ]
    for start, end in windows:
        rendered.append(f"\n--- lines {start}-{end} ---")
        rendered.extend(lines[start - 1 : end])
        if len("\n".join(rendered)) >= token_limit * 4:
            break
    result = "\n".join(rendered)
    return result[: token_limit * 4]
