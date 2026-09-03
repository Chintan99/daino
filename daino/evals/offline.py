"""The eval kinds that need no model: retrieval ranking, and sizing arithmetic.

These are the ones worth running on every change, because they are free and they
cover exactly the code that has no other test: the ranking constants in
:mod:`daino.context.retrieval`, and the thresholds in
:mod:`daino.context.profiles` that decide whether a task is too big for a model.

Both were hand-tuned against real failures and then had no way to be checked. The
next person to adjust ``_LEXICAL`` or ``_MIN_WORKING_FRACTION`` could only find
out they had broken something by watching an agent behave worse a week later.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from time import monotonic

from daino.config.models import ModelProfileConfig
from daino.context.profiles import CapabilityEnvelope, ModelExecutionProfile
from daino.context.retrieval import select_candidates
from daino.evals.models import (
    CaseResult,
    EvalCase,
    RetrievalExpectation,
    SizingExpectation,
)
from daino.repository.graph import ImportGraph
from daino.schemas import RepositoryIndex, TaskSpec
from daino.schemas.core import RepositoryFile, RepositorySymbol

#: Extension -> the language name the indexer would record. Only the ones a
#: fixture realistically uses; anything else is indexed as plain text, which the
#: ranking treats the same way.
_LANGUAGES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".md": "markdown",
}


def synthetic_index(files: dict[str, str], root: str = "/eval") -> RepositoryIndex:
    """Build an index from inline file contents.

    Imports and symbols are parsed with the same crude rules the fixture format
    implies rather than by running the real indexer: a case is about the ranking,
    and making it depend on the language parsers would let an indexer change
    silently rewrite what a retrieval case is testing.
    """
    entries: list[RepositoryFile] = []
    languages: dict[str, int] = {}
    for path, content in sorted(files.items()):
        language = _language_for(path)
        languages[language] = languages.get(language, 0) + 1
        entries.append(
            RepositoryFile(
                path=path,
                language=language,
                size=len(content.encode()),
                digest=hashlib.sha256(content.encode()).hexdigest()[:16],
                summary=content.strip().splitlines()[0][:120] if content.strip() else "",
                imports=_imports(content, files),
                symbols=_symbols(path, content),
            )
        )
    return RepositoryIndex(
        root=root,
        # Fixed rather than "now": a case's inputs should be identical on every
        # run, and a timestamp is the one field that would not be.
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=entries,
        languages=languages,
    )


def _language_for(path: str) -> str:
    for suffix, language in _LANGUAGES.items():
        if path.endswith(suffix):
            return language
    return "text"


def _imports(content: str, files: dict[str, str]) -> list[str]:
    """Which fixture files this one references, by module-ish name.

    A fixture writes ``from billing.charges import x`` or
    ``import ./charges``; both resolve to a path in the fixture when one exists.
    Anything unresolvable is a third-party import and is dropped, which is what
    the real graph does too.
    """
    found: list[str] = []
    stems = {path: _stem(path) for path in files}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import ", "from ", "const ", "require(")):
            continue
        for path, stem in stems.items():
            if stem and stem in stripped and path not in found:
                found.append(path)
    return found


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _symbols(path: str, content: str) -> list[RepositorySymbol]:
    symbols: list[RepositorySymbol] = []
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        for prefix, kind in (
            ("def ", "function"),
            ("async def ", "function"),
            ("class ", "class"),
            ("function ", "function"),
            ("export function ", "function"),
            ("export class ", "class"),
        ):
            if not stripped.startswith(prefix):
                continue
            name = stripped[len(prefix) :].split("(")[0].split(":")[0].split("{")[0].strip()
            if name:
                symbols.append(
                    RepositorySymbol(path=path, name=name, kind=kind, line=number)
                )
            break
    return symbols


def run_retrieval_case(case: EvalCase) -> CaseResult:
    """Rank the fixture against the case's task and check the expectations."""
    started = monotonic()
    expectation = case.retrieval or RetrievalExpectation()
    index = synthetic_index(case.files)
    graph = ImportGraph.build(index)
    task = TaskSpec(
        id=case.id,
        title=case.description or case.id,
        objective=case.instruction,
        allowed_files=list(case.required),
        relevant_symbols=list(case.symbols),
        acceptance_criteria=[],
        verification_commands=[],
    )
    candidates = select_candidates(index, task, list(case.required), graph)
    selected = [candidate.path for candidate in candidates]
    failures: list[str] = []
    for path in expectation.includes:
        if path not in selected:
            failures.append(
                f"{path} was not selected; the ranking chose {selected[:8] or 'nothing'}"
            )
    for path in expectation.excludes:
        if path in selected:
            failures.append(f"{path} was selected at position {selected.index(path) + 1}")
    rank_of = {path: rank for rank, path in enumerate(selected)}
    head = selected[: expectation.top_n]
    for path in expectation.top:
        if path not in head:
            placing = rank_of.get(path)
            failures.append(
                f"{path} was expected in the top {expectation.top_n} but ranked "
                + (f"{placing + 1}" if placing is not None else "nowhere")
            )
    for rule in expectation.order:
        higher, _, lower = rule.partition(">")
        higher, lower = higher.strip(), lower.strip()
        if not higher or not lower:
            failures.append(f"malformed order rule {rule!r}; expected 'a.py > b.py'")
            continue
        if higher not in rank_of:
            failures.append(f"{higher} was not selected at all, so it cannot outrank {lower}")
        elif lower in rank_of and rank_of[higher] >= rank_of[lower]:
            failures.append(
                f"{higher} ranked {rank_of[higher] + 1} but {lower} ranked "
                f"{rank_of[lower] + 1}; the order is inverted"
            )
    if expectation.max_selected and len(selected) > expectation.max_selected:
        failures.append(
            f"selected {len(selected)} files, more than the {expectation.max_selected} expected"
        )
    return CaseResult(
        case_id=case.id,
        kind=case.kind,
        passed=not failures,
        failures=failures,
        duration_seconds=monotonic() - started,
    )


def run_sizing_case(case: EvalCase) -> CaseResult:
    """Derive an envelope from the case's profile and check its numbers."""
    started = monotonic()
    expectation = case.sizing or SizingExpectation()
    try:
        profile_config = ModelProfileConfig.model_validate(case.profile)
    except Exception as exc:  # noqa: BLE001 - a bad fixture is a case error
        return CaseResult(
            case_id=case.id,
            kind=case.kind,
            passed=False,
            error=f"profile is not valid: {exc}",
            duration_seconds=monotonic() - started,
        )
    execution = ModelExecutionProfile.resolve(
        case.profile.get("name", case.id),
        profile_config,
        input_budget_tokens=int(case.profile.get("input_budget_tokens", 0))
        or profile_config.context_window,
        project_budget_tokens=int(case.profile.get("project_budget_tokens", 24_000)),
        memory_items=int(case.profile.get("memory_items", 8)),
        memory_tokens=int(case.profile.get("memory_tokens", 2_000)),
    )
    envelope = CapabilityEnvelope.from_profile(execution)
    failures: list[str] = []
    _expect_flag(failures, "compact", expectation.compact, envelope.compact)
    _expect_flag(
        failures,
        "one_action_per_turn",
        expectation.one_action_per_turn,
        envelope.one_action_per_turn,
    )
    _expect_range(
        failures,
        "working_headroom_tokens",
        envelope.working_headroom_tokens,
        expectation.min_working_headroom_tokens,
        expectation.max_working_headroom_tokens,
    )
    _expect_range(
        failures,
        "max_files_per_task",
        envelope.max_files_per_task,
        expectation.min_max_files_per_task,
        expectation.max_max_files_per_task,
    )
    _expect_range(
        failures,
        "task_source_budget_tokens",
        envelope.task_source_budget_tokens,
        expectation.min_task_source_budget_tokens,
        expectation.max_task_source_budget_tokens,
    )
    return CaseResult(
        case_id=case.id,
        kind=case.kind,
        passed=not failures,
        failures=failures,
        duration_seconds=monotonic() - started,
    )


def _expect_flag(
    failures: list[str], name: str, expected: bool | None, actual: bool
) -> None:
    if expected is not None and expected != actual:
        failures.append(f"{name} is {actual}, expected {expected}")


def _expect_range(
    failures: list[str], name: str, actual: int, minimum: int, maximum: int
) -> None:
    if minimum and actual < minimum:
        failures.append(f"{name} is {actual:,}, below the expected minimum {minimum:,}")
    if maximum and actual > maximum:
        failures.append(f"{name} is {actual:,}, above the expected maximum {maximum:,}")
