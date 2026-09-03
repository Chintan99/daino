"""Hard ceilings on what one run may spend.

Every model call already records what it cost — ``ModelCall`` carries tokens,
cached tokens, latency and the provider-reported charge — and nothing ever read
those numbers back. The stall guard is the only thing that bounds a run, and it
bounds the *unproductive* case: an agent that keeps making genuine, non-repeating
progress on a task it will never finish spends money until the user notices.

So the recorded numbers become enforced ones. Three dimensions, because they
fail differently:

* **Tokens** always work. Every provider reports usage, so a token ceiling is
  the one limit that binds on a local Ollama and a hosted API alike.
* **Cost** binds only where a provider reports a charge (OpenRouter does; a
  self-hosted vLLM has no price to report). A cost ceiling is therefore a real
  guard on hosted routes and silently inert on local ones — which is the right
  behaviour, not a gap, since a local run has no bill.
* **Call count** is the crude backstop that binds everywhere, including on a
  provider whose usage reporting is broken.

Accounting is *after the fact*: a call is admitted when the budget is not yet
exhausted, and the reply that overshoots is paid for. Pre-authorising an unknown
output size would mean refusing calls that would have fit. The overshoot is one
call, which is the smallest granularity the design can have.

Budgets are per mission, not per gateway. A gateway is rebuilt for every pinned
profile — ``with_profile`` returns a new one — and a team fans out several
gateways over the same mission. Keying the ledger by mission id is what makes
nine concurrent reviewers share one ceiling instead of getting nine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from daino.config.models import BudgetConfig
from daino.events import BudgetExhausted, BudgetWarning, EventBus


class BudgetExceeded(RuntimeError):
    """A run hit its configured ceiling and must stop.

    A ``RuntimeError`` subclass for the same reason ``IncompleteRun`` is: every
    existing handler in the mission and chat paths already catches that, so an
    exhausted budget surfaces as a stopped run with a reason rather than an
    unhandled crash.
    """

    def __init__(self, dimension: str, spent: float, limit: float, *, mission_id: str = "") -> None:
        self.dimension = dimension
        self.spent = spent
        self.limit = limit
        self.mission_id = mission_id
        super().__init__(describe_exhaustion(dimension, spent, limit))


def describe_exhaustion(dimension: str, spent: float, limit: float) -> str:
    """One sentence a user can act on, naming the setting that raised the wall."""
    if dimension == "cost":
        return (
            f"Run stopped at the spend ceiling: ${spent:.4f} of ${limit:.4f} used. "
            "Raise budget.max_cost_usd in .daino/config.yaml, or narrow the request."
        )
    if dimension == "tokens":
        return (
            f"Run stopped at the token ceiling: {int(spent):,} of {int(limit):,} used. "
            "Raise budget.max_total_tokens in .daino/config.yaml, or narrow the request."
        )
    return (
        f"Run stopped at the model-call ceiling: {int(spent):,} of {int(limit):,} calls. "
        "Raise budget.max_model_calls in .daino/config.yaml, or narrow the request."
    )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """What a run has spent so far, for reporting and for the UI."""

    cost_usd: float = 0.0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    max_cost_usd: float = 0.0
    max_total_tokens: int = 0
    max_model_calls: int = 0
    exhausted: str = ""

    @property
    def limited(self) -> bool:
        return bool(self.max_cost_usd or self.max_total_tokens or self.max_model_calls)

    def fraction_used(self) -> float:
        """How close the tightest configured ceiling is to being hit, 0.0–1.0+."""
        fractions = [
            self.cost_usd / self.max_cost_usd if self.max_cost_usd else 0.0,
            self.total_tokens / self.max_total_tokens if self.max_total_tokens else 0.0,
            self.model_calls / self.max_model_calls if self.max_model_calls else 0.0,
        ]
        return max(fractions)


@dataclass
class RunBudget:
    """Running totals for one mission, checked before each call and updated after.

    Locked rather than plain attributes: a team wave runs its members
    concurrently against the same budget, and while asyncio would serialise the
    arithmetic on one loop, ``TeamRunner`` and the QA fan-out both reach this
    from threads too.
    """

    config: BudgetConfig
    mission_id: str = ""
    events: EventBus | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    #: Set once the first ceiling is crossed, so the reason survives for the
    #: report even though later checks would raise the same way.
    exhausted: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _warned: bool = field(default=False, repr=False, compare=False)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check(self) -> None:
        """Raise if a further model call would exceed a configured ceiling."""
        with self._lock:
            breach = self._breach()
        if breach is None:
            return
        dimension, spent, limit = breach
        if not self.exhausted:
            self.exhausted = dimension
            if self.events is not None:
                self.events.publish(
                    BudgetExhausted(
                        mission_id=self.mission_id or None,
                        dimension=dimension,
                        spent=float(spent),
                        limit=float(limit),
                        message=describe_exhaustion(dimension, spent, limit),
                    )
                )
        raise BudgetExceeded(dimension, spent, limit, mission_id=self.mission_id)

    def _breach(self) -> tuple[str, float, float] | None:
        """The first ceiling already reached, or ``None``. Caller holds the lock."""
        config = self.config
        if config.max_cost_usd and self.cost_usd >= config.max_cost_usd:
            return ("cost", self.cost_usd, config.max_cost_usd)
        if config.max_total_tokens and self.total_tokens >= config.max_total_tokens:
            return ("tokens", float(self.total_tokens), float(config.max_total_tokens))
        if config.max_model_calls and self.model_calls >= config.max_model_calls:
            return ("calls", float(self.model_calls), float(config.max_model_calls))
        return None

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Charge one completed call against the budget."""
        with self._lock:
            self.model_calls += 1
            self.input_tokens += max(0, input_tokens)
            self.output_tokens += max(0, output_tokens)
            self.cached_tokens += max(0, cached_tokens)
            self.cost_usd += max(0.0, cost)
            warn = self._should_warn()
            if warn:
                self._warned = True
            snapshot = self._snapshot()
        if warn and self.events is not None:
            self.events.publish(
                BudgetWarning(
                    mission_id=self.mission_id or None,
                    fraction=snapshot.fraction_used(),
                    cost_usd=snapshot.cost_usd,
                    total_tokens=snapshot.total_tokens,
                    model_calls=snapshot.model_calls,
                    message=(
                        f"This run has used {snapshot.fraction_used():.0%} of its configured "
                        "budget."
                    ),
                )
            )

    def _should_warn(self) -> bool:
        """Whether this call crossed the warning threshold. Caller holds the lock."""
        if self._warned:
            return False
        threshold = self.config.warn_at_fraction
        if threshold <= 0:
            return False
        return self._snapshot().fraction_used() >= threshold

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            cost_usd=self.cost_usd,
            total_tokens=self.total_tokens,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            model_calls=self.model_calls,
            max_cost_usd=self.config.max_cost_usd,
            max_total_tokens=self.config.max_total_tokens,
            max_model_calls=self.config.max_model_calls,
            exhausted=self.exhausted,
        )


class BudgetLedger:
    """Every live run's budget, keyed by mission.

    Shared across the gateways a mission produces, which is the whole point:
    ``with_profile`` hands out a new gateway per pinned profile and a team hands
    one to every member. They must all draw on the same account.
    """

    def __init__(self, config: BudgetConfig, events: EventBus | None = None) -> None:
        self.config = config
        self.events = events
        self._budgets: dict[str, RunBudget] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Whether any ceiling is configured. Nothing is tracked when none is."""
        return bool(
            self.config.max_cost_usd
            or self.config.max_total_tokens
            or self.config.max_model_calls
        )

    def budget_for(self, mission_id: str) -> RunBudget | None:
        """The budget for one mission, created on first use."""
        if not self.enabled or not mission_id:
            return None
        with self._lock:
            existing = self._budgets.get(mission_id)
            if existing is None:
                existing = RunBudget(
                    config=self.config, mission_id=mission_id, events=self.events
                )
                self._budgets[mission_id] = existing
            return existing

    def snapshot(self, mission_id: str) -> BudgetSnapshot | None:
        with self._lock:
            budget = self._budgets.get(mission_id)
        return budget.snapshot() if budget is not None else None

    def release(self, mission_id: str) -> BudgetSnapshot | None:
        """Drop a finished mission's budget and return its final state.

        Called when a mission ends, so a long-lived process does not accumulate
        one entry per turn for the life of the session.
        """
        with self._lock:
            budget = self._budgets.pop(mission_id, None)
        return budget.snapshot() if budget is not None else None
