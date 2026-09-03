"""Keep provider connections warm across model calls.

Every gateway method built a provider, used it once, and closed it. Closing an
``httpx.AsyncClient`` tears down its connection pool, so the next call — often
milliseconds later, to the same endpoint — paid for a fresh TCP handshake and a
fresh TLS negotiation. On a hosted API across the Atlantic that is a couple of
hundred milliseconds on *every step of every loop*, and an agent turn is dozens
of steps. It also re-resolved the API key each time, which on a keyring-backed
secret can mean a keychain prompt's worth of work per call.

This is a **checkout** pool, not a shared-instance cache, and the distinction is
the whole design. A provider adapter carries per-call state — the usage the
gateway reads back after the request, and the reasoning handler that forwards
chunks onto one mission's event bus. Handing the same instance to two concurrent
callers would cross-attribute both. So each borrower gets exclusive use of an
instance; what is shared and kept alive is the *connection pool inside it*.

Two bounds keep a long-lived process honest:

* **Idle TTL.** An instance nobody has borrowed for a while is closed, so a
  session that stops talking to a provider stops holding sockets open to it.
* **Maximum lifetime.** An instance is retired regardless of use after
  ``max_lifetime_seconds``. This is what bounds credential staleness: the pool
  keys on the secret *reference* (``env://OPENROUTER_API_KEY``) rather than on
  the resolved value, because resolving a keyring secret on every acquire would
  give back the cost this exists to remove. A key rotated in place is therefore
  picked up within one lifetime rather than instantly, which is the trade this
  makes deliberately.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from daino.config.models import ProviderConfig
from daino.providers.base import LLMProvider
from daino.providers.factory import create_provider

#: How long an unborrowed instance is kept before its sockets are released.
DEFAULT_IDLE_TTL_SECONDS = 300.0
#: Hard ceiling on one instance's age, borrowed or not.
DEFAULT_MAX_LIFETIME_SECONDS = 900.0
#: Idle instances retained per configuration. A team wave is the widest
#: concurrent fan-out Daino produces, so this is sized to keep one warm per
#: member rather than to cache the world.
DEFAULT_MAX_IDLE_PER_KEY = 8

ProviderFactory = Callable[[str, ProviderConfig], LLMProvider]


def fingerprint(
    name: str, config: ProviderConfig, factory: ProviderFactory | None = None
) -> tuple[object, ...]:
    """Everything that would make two providers behave differently.

    Anything that changes the wire request or the connection has to be in here:
    two profiles that differ only by ``reasoning_effort`` must not share an
    instance, because the adapter bakes that into the payload it builds.

    The factory is part of the identity for the same reason. Two callers that
    build adapters differently — the real one, and a test that substitutes a
    double — are asking for different objects even from identical config, and a
    pool that ignored that would hand one caller the other's provider.
    """
    return (
        factory,
        name,
        config.type,
        config.base_url.rstrip("/"),
        config.model,
        # The reference, never the resolved secret. See the module docstring for
        # why, and for what bounds the staleness that buys.
        config.api_key,
        config.timeout,
        config.max_retries,
        config.max_output_tokens,
        config.reasoning_effort,
        config.concurrency,
        config.application_name,
        config.referring_url,
        tuple(sorted(config.features)),
    )


@dataclass(slots=True)
class _Idle:
    provider: LLMProvider
    created: float
    returned: float


@dataclass
class PoolStats:
    """Counters for diagnostics and for the tests that prove reuse happens."""

    created: int = 0
    reused: int = 0
    retired: int = 0
    discarded: int = 0
    idle: int = 0
    live: int = 0


class ProviderPool:
    """Lends warm provider adapters, keyed by resolved configuration."""

    def __init__(
        self,
        *,
        factory: ProviderFactory | None = None,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        max_lifetime_seconds: float = DEFAULT_MAX_LIFETIME_SECONDS,
        max_idle_per_key: int = DEFAULT_MAX_IDLE_PER_KEY,
    ) -> None:
        self._factory = factory or create_provider
        self._idle_ttl = idle_ttl_seconds
        self._max_lifetime = max_lifetime_seconds
        self._max_idle = max_idle_per_key
        self._idle: dict[tuple[object, ...], list[_Idle]] = {}
        #: Instances currently checked out, so ``aclose`` can wait for them and
        #: ``release`` can find an instance's key without the caller carrying it.
        self._borrowed: dict[int, tuple[tuple[object, ...], float]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.stats = PoolStats()

    async def acquire(
        self,
        name: str,
        config: ProviderConfig,
        *,
        factory: ProviderFactory | None = None,
    ) -> LLMProvider:
        """Borrow an adapter for exclusive use until it is released.

        ``factory`` overrides how a *new* instance is built for this call only;
        it is part of the pool key, so an override never receives an adapter
        built by a different one.
        """
        build = factory or self._factory
        key = fingerprint(name, config, build)
        now = time.monotonic()
        async with self._lock:
            entries = self._idle.get(key, [])
            while entries:
                entry = entries.pop()
                if self._expired(entry, now):
                    await self._retire(entry.provider)
                    continue
                self._borrowed[id(entry.provider)] = (key, entry.created)
                self.stats.reused += 1
                self._refresh_counts()
                _prepare(entry.provider)
                return entry.provider
            self._idle.pop(key, None)
        provider = build(name, config)
        async with self._lock:
            self._borrowed[id(provider)] = (key, now)
            self.stats.created += 1
            self._refresh_counts()
        _prepare(provider)
        return provider

    async def release(self, provider: LLMProvider, *, discard: bool = False) -> None:
        """Return an adapter. ``discard`` closes it instead of keeping it warm.

        A caller that saw a transport-level failure should discard: the socket
        it just failed on is exactly the one the next borrower would get.
        """
        # Detached before any await so a concurrent release cannot double-return.
        async with self._lock:
            entry = self._borrowed.pop(id(provider), None)
            self._refresh_counts()
        if entry is None:
            # Never issued by this pool (a hand-built double in a test, or an
            # already-released instance). Closing it is the safe reading.
            await self._retire(provider)
            return
        key, created = entry
        _prepare(provider)
        now = time.monotonic()
        too_old = now - created >= self._max_lifetime
        if discard or too_old or self._closed:
            await self._retire(provider)
            return
        async with self._lock:
            entries = self._idle.setdefault(key, [])
            entries.append(_Idle(provider=provider, created=created, returned=now))
            overflow = entries[: max(0, len(entries) - self._max_idle)]
            del entries[: len(overflow)]
            self._refresh_counts()
        for stale in overflow:
            await self._retire(stale.provider)

    @asynccontextmanager
    async def lease(
        self,
        name: str,
        config: ProviderConfig,
        *,
        factory: ProviderFactory | None = None,
    ) -> AsyncIterator[LLMProvider]:
        """Borrow for the duration of a block, discarding on an unexpected error."""
        provider = await self.acquire(name, config, factory=factory)
        discard = False
        try:
            yield provider
        except BaseException:
            discard = True
            raise
        finally:
            await self.release(provider, discard=discard)

    async def prune(self) -> int:
        """Close idle instances past their TTL or lifetime. Returns how many."""
        now = time.monotonic()
        expired: list[LLMProvider] = []
        async with self._lock:
            for key, entries in list(self._idle.items()):
                keep = []
                for entry in entries:
                    if self._expired(entry, now):
                        expired.append(entry.provider)
                    else:
                        keep.append(entry)
                if keep:
                    self._idle[key] = keep
                else:
                    self._idle.pop(key, None)
            self._refresh_counts()
        for provider in expired:
            await self._retire(provider)
        return len(expired)

    async def aclose(self) -> None:
        """Close every idle instance and refuse to keep future returns warm."""
        async with self._lock:
            self._closed = True
            entries = [entry for group in self._idle.values() for entry in group]
            self._idle.clear()
            self._refresh_counts()
        for entry in entries:
            await self._retire(entry.provider)

    def _expired(self, entry: _Idle, now: float) -> bool:
        return (now - entry.returned) >= self._idle_ttl or (
            now - entry.created
        ) >= self._max_lifetime

    async def _retire(self, provider: LLMProvider) -> None:
        self.stats.retired += 1
        try:
            await provider.close()
        except Exception:  # noqa: BLE001 - a failed close must not fail the call
            self.stats.discarded += 1

    def _refresh_counts(self) -> None:
        """Recompute the reported gauges. Caller holds the lock."""
        self.stats.idle = sum(len(group) for group in self._idle.values())
        self.stats.live = len(self._borrowed)


def _prepare(provider: LLMProvider) -> None:
    """Clear the per-call state a previous borrower left on an adapter.

    Both fields are why this pool lends exclusively rather than sharing. The
    reasoning handler is the dangerous one: left attached, a reused adapter would
    publish the next mission's reasoning onto the previous mission's event bus.
    """
    with suppress(Exception):  # a custom double need not implement it
        provider.set_reasoning_handler(None)
    reset = getattr(provider, "reset_usage", None)
    if callable(reset):
        reset()


#: Process-wide pool. Shared for the same reason the request gate is: a gateway
#: is rebuilt for every pinned profile and every turn, so a per-gateway pool
#: would spend its life empty — which is the state it exists to avoid.
POOL = ProviderPool()
