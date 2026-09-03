"""What an eval case is, and what counts as passing one.

Daino's unit tests are strong on mechanism: they prove the loop applies actions,
the compactor sheds the right things, the gate refuses the right commands. None
of them measure whether the agent *finishes the task*, and none of them measure
whether the retrieval ranking picks the right files. Those are the two numbers
that actually change when someone touches the context work, and they were the two
nobody could see.

Three kinds of case, deliberately different in cost:

* **retrieval** — no model, no network, milliseconds. A synthetic repository, a
  task, and an assertion about which files the ranking chooses. This is what
  turns the hand-tuned constants in :mod:`daino.context.retrieval` from magic
  numbers into a thing with a regression test.
* **sizing** — no model either. A model profile in, and assertions about the
  envelope derived from it. The thresholds that decide whether a task gets split
  are arithmetic, and arithmetic can be checked.
* **task** — the expensive one. A scratch repository, a real instruction, a real
  model, and assertions about the resulting working tree. This is what "does
  this model actually work with Daino" means, and it is the only kind that
  cannot be run in CI for free.

The split matters because the cheap two are the ones that will actually get run
on every change, and they cover exactly the code the context fix touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CaseKind = Literal["retrieval", "sizing", "task"]


class RetrievalExpectation(BaseModel):
    """What the ranking should and should not have chosen."""

    #: Paths that must appear at all, in any position.
    includes: list[str] = Field(default_factory=list)
    #: Paths that must not appear. The more informative half: a ranking that
    #: pulls in an unrelated test file is failing in a way ``includes`` cannot see.
    excludes: list[str] = Field(default_factory=list)
    #: Paths that must appear within the first ``top_n`` results. Position is
    #: what the constants control, so an ordering assertion is what tests them.
    top: list[str] = Field(default_factory=list)
    top_n: int = Field(default=3, ge=1)
    #: ``"a.py > b.py"`` — a.py must rank strictly above b.py. The most useful
    #: assertion for a ranking, because most of the constants control *order*
    #: rather than membership: an unrelated test file usually is in the list,
    #: and what the penalty buys is that it is in the list *below* the relevant
    #: one, where the budget never reaches it.
    order: list[str] = Field(default_factory=list)
    #: Upper bound on how many files the selection returns.
    max_selected: int = Field(default=0, ge=0)


class SizingExpectation(BaseModel):
    """Bounds on the numbers derived from a model profile."""

    compact: bool | None = None
    one_action_per_turn: bool | None = None
    min_working_headroom_tokens: int = Field(default=0, ge=0)
    max_working_headroom_tokens: int = Field(default=0, ge=0)
    min_max_files_per_task: int = Field(default=0, ge=0)
    max_max_files_per_task: int = Field(default=0, ge=0)
    min_task_source_budget_tokens: int = Field(default=0, ge=0)
    max_task_source_budget_tokens: int = Field(default=0, ge=0)


class TaskExpectation(BaseModel):
    """What a finished end-to-end run must have produced."""

    #: Repository-relative paths the run must have changed.
    changed: list[str] = Field(default_factory=list)
    #: Paths it must not have touched. Catches an agent that "succeeded" by
    #: rewriting the test instead of the code.
    unchanged: list[str] = Field(default_factory=list)
    #: ``path -> regex`` that the file's final contents must match.
    contains: dict[str, str] = Field(default_factory=dict)
    #: ``path -> regex`` that must *not* match.
    absent: dict[str, str] = Field(default_factory=dict)
    #: Commands that must exit zero afterwards. The strongest assertion
    #: available, and the one that most resembles what the user cares about.
    commands: list[str] = Field(default_factory=list)
    #: Regex the agent's own answer must match, for question-shaped cases.
    answer_matches: str = ""
    #: Ceiling on actions taken. A run that needed forty steps for a one-line
    #: change passed the assertion and failed the point of it.
    max_steps: int = Field(default=0, ge=0)


class EvalCase(BaseModel):
    """One measurable thing, of one of the three kinds."""

    id: str
    kind: CaseKind
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    #: Synthetic repository: ``path -> contents``. Used by every kind — written
    #: to a scratch directory for a task case, indexed in memory for a retrieval
    #: one — so a suite reads the same way whichever kind it holds.
    files: dict[str, str] = Field(default_factory=dict)

    #: retrieval + task: what the agent is asked to do.
    instruction: str = ""
    #: retrieval: the task's declared scope, seeded but not itself ranked.
    required: list[str] = Field(default_factory=list)
    #: retrieval: symbols the planner named.
    symbols: list[str] = Field(default_factory=list)
    retrieval: RetrievalExpectation | None = None

    #: sizing: the profile to derive an envelope from.
    profile: dict[str, Any] = Field(default_factory=dict)
    sizing: SizingExpectation | None = None

    #: task: assertions about the working tree afterwards.
    expect: TaskExpectation | None = None
    #: task: seconds before the run is abandoned.
    timeout_seconds: float = Field(default=900.0, gt=0)

    @model_validator(mode="after")
    def require_the_right_parts(self) -> EvalCase:
        if self.kind == "retrieval" and self.retrieval is None:
            raise ValueError(f"{self.id}: a retrieval case needs a 'retrieval' block")
        if self.kind == "sizing" and self.sizing is None:
            raise ValueError(f"{self.id}: a sizing case needs a 'sizing' block")
        if self.kind == "task":
            if self.expect is None:
                raise ValueError(f"{self.id}: a task case needs an 'expect' block")
            if not self.instruction:
                raise ValueError(f"{self.id}: a task case needs an 'instruction'")
        return self

    @property
    def needs_a_model(self) -> bool:
        return self.kind == "task"


class EvalSuite(BaseModel):
    """A named collection of cases."""

    name: str
    description: str = ""
    cases: list[EvalCase] = Field(default_factory=list)


@dataclass(slots=True)
class CaseResult:
    """What happened to one case, and why it failed if it did."""

    case_id: str
    kind: str
    passed: bool
    #: Every assertion that did not hold, phrased so the number is actionable.
    failures: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    #: Populated for task cases: what the run cost and how long it took.
    steps: int = 0
    model_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    #: Set when the case could not run at all, as distinct from failing.
    error: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.error)


@dataclass(slots=True)
class SuiteResult:
    """Every case in one suite, against one model."""

    suite: str
    #: Empty for the model-free kinds.
    model: str = ""
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.results if item.passed)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if not item.passed and not item.skipped)

    @property
    def errored(self) -> int:
        return sum(1 for item in self.results if item.skipped)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def success_rate(self) -> float:
        """Passes over cases that actually ran. ``0.0`` when none did.

        Errored cases are excluded from the denominator rather than counted as
        failures: a provider outage is not a capability measurement, and folding
        it into the score is how a benchmark starts lying.
        """
        ran = self.total - self.errored
        return self.passed / ran if ran else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(item.cost_usd for item in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(item.total_tokens for item in self.results)


def matches(pattern: str, text: str) -> bool:
    """Regex search, tolerating a pattern that does not compile.

    A broken pattern reports as "did not match" rather than raising, because the
    caller is already collecting failures and a suite with one bad regex should
    still report the other forty cases.
    """
    try:
        return re.search(pattern, text, re.MULTILINE) is not None
    except re.error:
        return False
