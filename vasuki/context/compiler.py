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
        candidates: list[str] = []
        candidates.extend(task.expected_files)
        candidates.extend(task.allowed_files)
        lower_terms = {
            word.lower() for word in f"{task.title} {task.objective}".split() if len(word) > 3
        }
        for item in index.files:
            haystack = f"{item.path} {item.summary}".lower()
            if any(term in haystack for term in lower_terms):
                candidates.append(item.path)
        for test in self.indexer.tests():
            stem = Path(test).stem.removeprefix("test_")
            if any(stem in path for path in candidates):
                candidates.append(test)

        files: dict[str, str] = {}
        tests: dict[str, str] = {}
        used = self._estimate_tokens(task.model_dump_json())
        for relative in dict.fromkeys(candidates):
            path = (self.root / relative).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            cost = self._estimate_tokens(content)
            if used + cost > self.token_budget:
                continue
            target = tests if "test" in path.name.lower() or "tests" in path.parts else files
            target[relative] = content
            used += cost
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
