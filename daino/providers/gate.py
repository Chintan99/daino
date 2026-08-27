"""Serialise requests to a provider that can only serve one at a time.

A local runtime holds one copy of one model. Asking it for three completions at
once does not make it three times faster: the requests queue inside the server —
or worse, are run in parallel against a split KV cache — and each one arrives
slower than it would have alone. The client's own timeout is running the whole
time it waits, so a fan-out that would have finished serially can time out
instead.

Nothing here limits a hosted provider by default: OpenRouter answers concurrent
requests from separate capacity, and serialising those would only make a QA scan
take five times longer for no reason.

The gate is keyed by *endpoint* and held at module level, not on the adapter: a
fresh adapter is built for every model call (see ``daino.agents.gateway``), so a
per-instance lock would serialise nothing — and the thing that can only serve one
request at a time is the model server, so two configuration entries pointing at
the same Ollama share its queue while two Ollamas on different hosts do not
block each other.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

#: (endpoint, event loop id) -> semaphore. The loop is part of the key
#: because an asyncio primitive belongs to the loop that first awaited it, and a
#: test suite runs each case on a fresh loop.
_gates: dict[tuple[str, int], asyncio.Semaphore] = {}
_limits: dict[tuple[str, int], int] = {}


def _slot(endpoint: str, limit: int) -> asyncio.Semaphore:
    key = (endpoint, id(asyncio.get_running_loop()))
    if _limits.get(key) != limit:
        # A changed limit (the user edited the provider) replaces the gate. In
        # flight requests keep the old one, which is correct: they already hold
        # a slot on it.
        _gates[key] = asyncio.Semaphore(limit)
        _limits[key] = limit
    return _gates[key]


@contextlib.asynccontextmanager
async def request_slot(endpoint: str, limit: int) -> AsyncIterator[None]:
    """Hold a request slot for ``endpoint``, or pass through when unlimited.

    ``limit <= 0`` means "no limit" — the hosted default.
    """
    if limit <= 0:
        yield
        return
    async with _slot(endpoint, limit):
        yield


def reset() -> None:
    """Drop every gate. For tests, and for a configuration reload."""
    _gates.clear()
    _limits.clear()


def active_limits() -> dict[str, int]:
    """The configured limit per endpoint, for diagnostics."""
    return {endpoint: limit for (endpoint, _), limit in _limits.items()}
