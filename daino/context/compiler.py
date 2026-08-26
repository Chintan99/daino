"""Budgeted, task-specific context compilation."""

from __future__ import annotations

from pathlib import Path

from daino.repository import RepositoryIndexer
from daino.schemas import ContextBundle, TaskSpec


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
        discovered: list[str] = []
        lower_terms = {
            word.lower() for word in f"{task.title} {task.objective}".split() if len(word) > 3
        }
        indexed_files = {item.path: item for item in index.files}
        for item in index.files:
            haystack = f"{item.path} {item.summary}".lower()
            if any(term in haystack for term in lower_terms):
                discovered.append(item.path)
        for test in self.indexer.tests():
            stem = Path(test).stem.removeprefix("test_")
            if any(stem in path for path in [*required, *discovered]):
                discovered.append(test)

        files: dict[str, str] = {}
        tests: dict[str, str] = {}
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
                    return
                # Truncate rather than omit, and say so, so the agent knows it is
                # looking at part of the file instead of assuming it has all of it.
                remaining = max(0, self.token_budget - used) * 4
                if remaining < 400:
                    return
                content = content[:remaining] + "\n… file truncated to fit the context budget\n"
                cost = self._estimate_tokens(content)
            target = tests if "test" in path.name.lower() or "tests" in path.parts else files
            target[relative] = content
            used += cost

        for relative in required:
            include(relative, mandatory=True)
        for relative in dict.fromkeys(discovered):
            if self.max_files is not None and len(files) + len(tests) >= self.max_files:
                break
            include(relative, mandatory=False)
        return ContextBundle(
            task=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            architecture_decisions=decisions or [],
            files=files,
            tests=tests,
            failure_summary=failure_summary,
            token_estimate=used,
            included_paths=[*files, *tests],
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
