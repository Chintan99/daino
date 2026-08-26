"""Optional OpenTelemetry export initialization."""

from __future__ import annotations

from importlib import import_module


def configure_otel(endpoint: str | None, *, service_name: str = "daino") -> bool:
    """Configure OTLP/HTTP traces when the optional dependency and endpoint are present."""
    if not endpoint:
        return False
    try:
        exporter_module = import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        resources_module = import_module("opentelemetry.sdk.resources")
        trace_module = import_module("opentelemetry.sdk.trace")
        export_module = import_module("opentelemetry.sdk.trace.export")
        api_trace_module = import_module("opentelemetry.trace")
    except ImportError:
        return False
    provider = trace_module.TracerProvider(
        resource=resources_module.Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(
        export_module.BatchSpanProcessor(exporter_module.OTLPSpanExporter(endpoint=endpoint))
    )
    api_trace_module.set_tracer_provider(provider)
    return True
