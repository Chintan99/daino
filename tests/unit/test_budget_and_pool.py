"""Spend ceilings are enforced, and provider connections survive a call."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from daino.agents.budget import BudgetExceeded, BudgetLedger, RunBudget
from daino.agents.gateway import ModelGateway
from daino.config.models import BudgetConfig, ModelProfileConfig, ProviderConfig, Settings
from daino.events import BudgetExhausted, BudgetWarning, EventBus, MissionEvent
from daino.model_router import ModelRole
from daino.providers.base import ProviderUsage
from daino.providers.pool import ProviderPool
from daino.schemas import LLMResponse, Message


class RecordingDatabase:
    def __init__(self) -> None:
        self.records: list[Any] = []

    @contextmanager
    def session(self) -> Iterator[RecordingDatabase]:
        yield self

    def add(self, record: Any) -> None:
        self.records.append(record)


class CountingProvider:
    """A provider that reports fixed usage and counts how often it was closed."""

    def __init__(self, usage: ProviderUsage | None = None) -> None:
        self.calls = 0
        self.closed = 0
        self._usage = usage or ProviderUsage(input_tokens=1_000, output_tokens=500, cost=0.02)

    def supports_tools(self) -> bool:
        return False

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="ok", model="qwen", provider="local")

    async def close(self) -> None:
        self.closed += 1

    @property
    def last_usage(self) -> ProviderUsage:
        return self._usage

    def reset_usage(self) -> None:
        return None


def budgeted_settings(budget: BudgetConfig) -> Settings:
    settings = Settings()
    settings.providers = {
        "local": ProviderConfig(type="ollama", base_url="http://local/v1", model="qwen")
    }
    settings.models = {"profile": ModelProfileConfig(provider="local", model="qwen", local=True)}
    settings.routing = {"builder": "profile"}
    settings.budget = budget
    return settings


def test_unconfigured_budget_tracks_nothing() -> None:
    ledger = BudgetLedger(BudgetConfig())
    assert not ledger.enabled
    assert ledger.budget_for("mission-1") is None


def test_call_ceiling_admits_then_refuses() -> None:
    budget = RunBudget(config=BudgetConfig(max_model_calls=2), mission_id="m")
    budget.check()
    budget.record(input_tokens=10, output_tokens=5)
    budget.check()
    budget.record(input_tokens=10, output_tokens=5)
    with pytest.raises(BudgetExceeded) as raised:
        budget.check()
    assert raised.value.dimension == "calls"
    assert "model-call ceiling" in str(raised.value)


def test_token_ceiling_charges_the_overshooting_call() -> None:
    """The call that crosses the line is paid for; the next one is refused.

    Pre-authorising would mean refusing calls that would have fit, because the
    output size is unknown until the reply arrives.
    """
    budget = RunBudget(config=BudgetConfig(max_total_tokens=1_000), mission_id="m")
    budget.check()
    budget.record(input_tokens=900, output_tokens=400)
    assert budget.total_tokens == 1_300
    with pytest.raises(BudgetExceeded) as raised:
        budget.check()
    assert raised.value.dimension == "tokens"


def test_cost_ceiling_is_inert_without_reported_cost() -> None:
    """A local model reports no charge, so only a token ceiling can bind there."""
    budget = RunBudget(config=BudgetConfig(max_cost_usd=1.0), mission_id="m")
    for _ in range(50):
        budget.record(input_tokens=5_000, output_tokens=2_000, cost=0.0)
        budget.check()
    assert budget.cost_usd == 0.0


def test_warning_fires_once_and_exhaustion_publishes() -> None:
    events: list[MissionEvent] = []
    bus = EventBus()
    bus.subscribe(events.append)
    budget = RunBudget(
        config=BudgetConfig(max_model_calls=4, warn_at_fraction=0.5),
        mission_id="m",
        events=bus,
    )
    for _ in range(4):
        budget.record()
    warnings = [event for event in events if isinstance(event, BudgetWarning)]
    assert len(warnings) == 1
    with pytest.raises(BudgetExceeded):
        budget.check()
    exhausted = [event for event in events if isinstance(event, BudgetExhausted)]
    assert len(exhausted) == 1
    assert exhausted[0].dimension == "calls"
    # A second check raises again but does not re-announce.
    with pytest.raises(BudgetExceeded):
        budget.check()
    assert len([event for event in events if isinstance(event, BudgetExhausted)]) == 1


def test_ledger_shares_one_account_across_pinned_gateways() -> None:
    """``with_profile`` must not hand a pinned session a second allowance."""
    settings = budgeted_settings(BudgetConfig(max_model_calls=3))
    gateway = ModelGateway(settings, RecordingDatabase())  # type: ignore[arg-type]
    pinned = gateway.with_profile("profile")
    assert pinned.budgets is gateway.budgets
    gateway.budgets.budget_for("m").record()  # type: ignore[union-attr]
    assert pinned.budget_snapshot("m").model_calls == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_gateway_stops_at_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CountingProvider(ProviderUsage(input_tokens=600, output_tokens=0))
    monkeypatch.setattr("daino.agents.gateway.create_provider", lambda _n, _c: provider)
    gateway = ModelGateway(  # type: ignore[arg-type]
        budgeted_settings(BudgetConfig(max_total_tokens=1_000)),
        RecordingDatabase(),
    )
    messages = [Message(role="user", content="work")]
    await gateway.complete("m", ModelRole.BUILDER, messages)
    await gateway.complete("m", ModelRole.BUILDER, messages)
    with pytest.raises(BudgetExceeded):
        await gateway.complete("m", ModelRole.BUILDER, messages)
    # The refused call cost nothing: the provider was never asked.
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_released_budget_reports_final_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CountingProvider()
    monkeypatch.setattr("daino.agents.gateway.create_provider", lambda _n, _c: provider)
    gateway = ModelGateway(  # type: ignore[arg-type]
        budgeted_settings(BudgetConfig(max_cost_usd=10.0)),
        RecordingDatabase(),
    )
    await gateway.complete("m", ModelRole.BUILDER, [Message(role="user", content="work")])
    final = gateway.release_budget("m")
    assert final is not None
    assert final.model_calls == 1
    assert final.cost_usd == pytest.approx(0.02)
    assert gateway.budget_snapshot("m") is None


@pytest.mark.asyncio
async def test_pool_reuses_an_adapter_instead_of_reconnecting() -> None:
    built: list[CountingProvider] = []

    def factory(_name: str, _config: ProviderConfig) -> CountingProvider:
        provider = CountingProvider()
        built.append(provider)
        return provider

    pool = ProviderPool(factory=factory)  # type: ignore[arg-type]
    config = ProviderConfig(type="ollama", base_url="http://local/v1", model="qwen")
    first = await pool.acquire("local", config)
    await pool.release(first)
    second = await pool.acquire("local", config)
    assert second is first
    assert len(built) == 1
    assert first.closed == 0
    assert pool.stats.reused == 1
    # Still checked out, so closing the pool leaves it alone — an adapter with a
    # request in flight must not have its socket pulled. Releasing it into a
    # closed pool retires it instead of keeping it warm.
    await pool.aclose()
    assert first.closed == 0
    await pool.release(second)
    assert first.closed == 1


@pytest.mark.asyncio
async def test_pool_isolates_concurrent_borrowers() -> None:
    """Two callers must never share an adapter: usage would cross-attribute."""

    def factory(_name: str, _config: ProviderConfig) -> CountingProvider:
        return CountingProvider()

    pool = ProviderPool(factory=factory)  # type: ignore[arg-type]
    config = ProviderConfig(type="ollama", base_url="http://local/v1", model="qwen")
    first = await pool.acquire("local", config)
    second = await pool.acquire("local", config)
    assert first is not second
    await pool.release(first)
    await pool.release(second)
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_discards_after_a_transport_failure() -> None:
    def factory(_name: str, _config: ProviderConfig) -> CountingProvider:
        return CountingProvider()

    pool = ProviderPool(factory=factory)  # type: ignore[arg-type]
    config = ProviderConfig(type="ollama", base_url="http://local/v1", model="qwen")
    provider = await pool.acquire("local", config)
    await pool.release(provider, discard=True)
    assert provider.closed == 1
    replacement = await pool.acquire("local", config)
    assert replacement is not provider
    await pool.release(replacement)
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_retires_an_expired_idle_adapter() -> None:
    def factory(_name: str, _config: ProviderConfig) -> CountingProvider:
        return CountingProvider()

    pool = ProviderPool(factory=factory, idle_ttl_seconds=0.0)  # type: ignore[arg-type]
    config = ProviderConfig(type="ollama", base_url="http://local/v1", model="qwen")
    provider = await pool.acquire("local", config)
    await pool.release(provider)
    replacement = await pool.acquire("local", config)
    assert replacement is not provider
    assert provider.closed == 1
    await pool.release(replacement)
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_keys_on_configuration() -> None:
    def factory(_name: str, _config: ProviderConfig) -> CountingProvider:
        return CountingProvider()

    pool = ProviderPool(factory=factory)  # type: ignore[arg-type]
    first = await pool.acquire(
        "local", ProviderConfig(type="ollama", base_url="http://a/v1", model="qwen")
    )
    await pool.release(first)
    second = await pool.acquire(
        "local", ProviderConfig(type="ollama", base_url="http://b/v1", model="qwen")
    )
    assert second is not first
    await pool.release(second)
    await pool.aclose()


@pytest.mark.asyncio
async def test_gateway_returns_the_adapter_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: two model calls, one connection."""
    provider = CountingProvider()
    monkeypatch.setattr("daino.agents.gateway.create_provider", lambda _n, _c: provider)
    pool = ProviderPool()
    gateway = ModelGateway(  # type: ignore[arg-type]
        budgeted_settings(BudgetConfig()),
        RecordingDatabase(),
        pool=pool,
    )
    messages = [Message(role="user", content="work")]
    await gateway.complete("m", ModelRole.BUILDER, messages)
    await gateway.complete("m", ModelRole.BUILDER, messages)
    assert provider.calls == 2
    assert provider.closed == 0
    assert pool.stats.created == 1
    assert pool.stats.reused == 1
    await pool.aclose()
    assert provider.closed == 1
