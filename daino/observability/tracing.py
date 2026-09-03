"""Spans for the work that actually costs time and money.

``configure_otel`` set up an exporter and nothing ever produced a span, so the
only thing a configured collector received was a heartbeat. This module is the
other half: a ``span`` that agent code can wrap around a model call, a tool
execution, or a loop step, and which costs almost nothing when no collector is
configured.

Two properties matter more than coverage:

* **Tracing is never load-bearing.** Every entry point degrades to a no-op when
  the OpenTelemetry SDK is absent, when no endpoint is configured, or when the
  SDK itself raises. An agent must not fail a mission because a collector went
  away mid-run.
* **Attributes carry no payloads.** Spans record identity and shape — role,
  model, action, path, token counts, success — never prompts, file contents, or
  command output. Those already have a redacted audit ledger; a trace exporter
  is usually the least access-controlled sink in a deployment, and quietly
  shipping source code to it would be a leak dressed as observability.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any

#: Resolved once, on first use. ``None`` means "not yet looked up"; ``False``
#: means "looked up and unavailable", which is the state that has to stay cheap
#: because every span in an untraced process passes through it.
_TRACER: Any | None = None
_ENABLED = False

#: Span namespace. Kept as one prefix so a collector can select Daino's spans
#: without needing to know every operation name.
PREFIX = "daino."


class _NoopSpan:
    """Stand-in with the sliver of the span API this module's callers use."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102
        return None

    def set_attributes(self, values: Mapping[str, Any]) -> None:  # noqa: D102
        return None

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:  # noqa: D102
        return None

    def record_exception(self, exception: BaseException) -> None:  # noqa: D102
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None

    def is_recording(self) -> bool:  # noqa: D102
        return False


NOOP_SPAN = _NoopSpan()


def configure_tracing(endpoint: str | None, *, service_name: str = "daino") -> bool:
    """Install the exporter and arm :func:`span`.

    Returns whether tracing is live, so a caller can say so once at startup
    rather than leaving the operator guessing why a collector is empty.
    """
    global _TRACER, _ENABLED
    from daino.observability.otel import configure_otel

    if not configure_otel(endpoint, service_name=service_name):
        _TRACER = None
        _ENABLED = False
        return False
    try:
        from opentelemetry import trace as api_trace

        _TRACER = api_trace.get_tracer(service_name)
    except Exception:  # noqa: BLE001 - tracing must never break startup
        _TRACER = None
        _ENABLED = False
        return False
    _ENABLED = True
    return True


def tracing_enabled() -> bool:
    """Whether spans are currently being recorded."""
    return _ENABLED


def reset_tracing() -> None:
    """Forget the resolved tracer. For tests, and for a reconfigured endpoint."""
    global _TRACER, _ENABLED
    _TRACER = None
    _ENABLED = False


def resolve_endpoint(configured: str | None) -> str:
    """Pick the collector endpoint, preferring project configuration.

    Falling back to ``OTEL_EXPORTER_OTLP_ENDPOINT`` is what lets a deployment
    that already configures every other service through the standard variable
    get Daino's traces without editing a second file.
    """
    return (
        configured
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    )


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Record one operation, or do nothing at all when tracing is off.

    ``name`` is the operation, without the ``daino.`` prefix this adds. Keyword
    attributes with a ``None`` value are dropped rather than exported as nulls,
    which lets callers pass optional identity fields unconditionally.
    """
    if not _ENABLED or _TRACER is None:
        yield NOOP_SPAN
        return
    try:
        started = _TRACER.start_as_current_span(PREFIX + name)
    except Exception:  # noqa: BLE001 - a broken SDK must not break the agent
        yield NOOP_SPAN
        return
    with started as current:
        with suppress(Exception):  # attributes are never load-bearing
            set_attributes(current, attributes)
        try:
            yield current
        except BaseException as exc:
            # Recorded, then re-raised untouched. A span is an observer of the
            # failure, never a participant in how it propagates.
            with suppress(Exception):  # the original failure is what matters
                current.record_exception(exc)
                current.set_attribute("daino.failed", True)
            raise


def set_attributes(current: Any, attributes: Mapping[str, Any]) -> None:
    """Apply attributes to a span, skipping ``None`` and unexportable values."""
    if current is NOOP_SPAN:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            current.set_attribute(key, value)
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            current.set_attribute(key, list(value))
        else:
            current.set_attribute(key, str(value))
