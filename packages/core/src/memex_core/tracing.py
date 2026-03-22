"""OpenTelemetry tracing setup for Memex.

Configures OTLP export and auto-instruments LiteLLM (used by DSPy)
so that all LLM calls are captured as spans. Compatible with any
OTLP-compliant backend (Arize Phoenix, Jaeger, Grafana Tempo, etc.).
"""

import logging
from urllib.parse import urlparse

import httpx

from memex_common.config import TracingConfig

logger = logging.getLogger('memex.core.tracing')

_initialized = False


def setup_tracing(config: TracingConfig) -> None:
    """Initialize OpenTelemetry with OTLP exporter and LiteLLM auto-instrumentation.

    Idempotent — subsequent calls are no-ops.
    """
    global _initialized
    if _initialized:
        return

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from openinference.instrumentation.litellm import LiteLLMInstrumentor
    except ImportError as e:
        raise ImportError(
            'Tracing is enabled but required packages are not installed. '
            'Install them with: uv add memex-core[tracing]'
        ) from e

    resource = Resource.create({'service.name': config.service_name})
    exporter = OTLPSpanExporter(
        endpoint=config.endpoint,
        headers=config.headers or None,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Set as global tracer provider
    from opentelemetry import trace

    trace.set_tracer_provider(provider)

    # Auto-instrument LiteLLM (captures all DSPy LLM calls)
    LiteLLMInstrumentor().instrument(tracer_provider=provider)

    _initialized = True
    logger.info('OpenTelemetry tracing enabled, exporting to %s', config.endpoint)

    # Non-blocking connectivity check
    _check_endpoint_reachable(config.endpoint)


def _check_endpoint_reachable(endpoint: str) -> None:
    """Best-effort check that the OTLP endpoint is reachable at startup."""
    try:
        parsed = urlparse(endpoint)
        base = f'{parsed.scheme}://{parsed.netloc}'
        resp = httpx.get(base, timeout=3.0)
        resp.close()
    except Exception:
        logger.warning(
            'Tracing endpoint %s is unreachable — spans will be buffered and retried.',
            endpoint,
        )


def check_tracing_health() -> bool:
    """Return True if tracing is initialized and the endpoint was reachable last check."""
    if not _initialized:
        return False
    return True


def is_tracing_enabled() -> bool:
    """Return whether tracing has been initialized."""
    return _initialized
