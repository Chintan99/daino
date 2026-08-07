"""Budgeted, task-specific context compilation."""

from __future__ import annotations

from pathlib import Path

from vasuki.repository import RepositoryIndexer
from vasuki.schemas import ContextBundle, TaskSpec


class ContextCompiler:
    """Selects exact relevant code while keeping prompts below a hard budget."""

    def __init__(self, root: Path, indexer: RepositoryIndexer, token_budget: int = 24_000) -> None:
        self.root = root.resolve()
        self.indexer = indexer
        self.token_budget = token_budget

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
