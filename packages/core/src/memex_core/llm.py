import logging
import asyncio
import time
from typing import Any, TypeVar

import dspy
import litellm.exceptions

from memex_core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from memex_core.metrics import (
    LLM_CALLS_TOTAL,
    LLM_CALL_DURATION_SECONDS,
    CIRCUIT_BREAKER_REJECTIONS_TOTAL,
    CIRCUIT_BREAKER_STATE,
)

logger = logging.getLogger('memex.core.llm')

# Tracing helpers — no-ops when tracing deps not installed
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind as _SpanKind
    from openinference.instrumentation import using_attributes as _oi_using_attributes

    _tracer = _otel_trace.get_tracer('memex.llm')
except ImportError:
    _tracer = None  # type: ignore[assignment]
    _oi_using_attributes = None  # type: ignore[assignment]

T = TypeVar('T')

# Process-wide circuit breaker resolved at call time, so
# ``configure_circuit_breaker(...)`` at app startup is observed by every
# subsequent LLM call. Importers that bind this name at import time would see
# a stale reference; use ``get_circuit_breaker()`` instead.
_circuit_breaker = CircuitBreaker()

# Circuit breaker state encoding for Prometheus gauge
_STATE_VALUES = {'closed': 0, 'open': 1, 'half-open': 2}


def configure_circuit_breaker(breaker: CircuitBreaker) -> None:
    """Replace the module-level circuit breaker (called during app startup)."""
    global _circuit_breaker
    _circuit_breaker = breaker


def get_circuit_breaker() -> CircuitBreaker:
    """Return the module-level circuit breaker (useful for health checks)."""
    return _circuit_breaker


async def run_dspy_operation(
    lm: dspy.LM,
    predictor: dspy.Module,
    input_kwargs: dict[str, Any],
    semaphore: asyncio.Semaphore | None = None,
    operation_name: str = 'dspy',
    timeout: int = 180,
) -> Any:
    """
    Executes a DSPy predictor with circuit breaker and metrics.

    LLM call observability (token usage, latency, prompts) is handled by
    OpenTelemetry auto-instrumentation of LiteLLM when tracing is enabled.

    Args:
        lm: The DSPy LM instance to use.
        predictor: The configured DSPy predictor (or ChainOfThought/Program).
        input_kwargs: Dictionary of arguments to pass to the predictor.
        semaphore: Optional semaphore for concurrency control.

    Returns:
        The predictor result.
    """

    # Check circuit breaker before attempting the LLM call
    try:
        await _circuit_breaker.pre_call()
    except CircuitBreakerOpen:
        CIRCUIT_BREAKER_REJECTIONS_TOTAL.inc()
        LLM_CALLS_TOTAL.labels(status='rejected').inc()
        raise

    CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

    start = time.monotonic()

    # Shallow copy to isolate history for this specific call
    lm_ = lm.copy()

    async def _execute():
        # The provider-timeout family is caught inside _execute so the outer
        # handler can distinguish "provider socket deadline" (httpx/LiteLLM)
        # from "asyncio.wait_for fired around _execute" (Python-side stall).
        if _tracer is not None and _oi_using_attributes is not None:
            with _tracer.start_as_current_span(operation_name, kind=_SpanKind.INTERNAL):
                with _oi_using_attributes(metadata={'memex.stage': operation_name}):
                    with dspy.context(lm=lm_):
                        if hasattr(predictor, 'acall'):
                            return await predictor.acall(**input_kwargs)
                        else:
                            # dead path: DSPy 3.1+ always exposes acall.
                            return await asyncio.to_thread(predictor, **input_kwargs)
        else:
            with dspy.context(lm=lm_):
                if hasattr(predictor, 'acall'):
                    return await predictor.acall(**input_kwargs)
                else:
                    # dead path: DSPy 3.1+ always exposes acall.
                    return await asyncio.to_thread(predictor, **input_kwargs)

    try:
        if semaphore:
            async with semaphore:
                result = await asyncio.wait_for(_execute(), timeout=timeout)
        else:
            result = await asyncio.wait_for(_execute(), timeout=timeout)

        await _circuit_breaker.record_success()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='success').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        # Clear LM history to prevent memory accumulation
        if hasattr(lm_, 'history'):
            lm_.history.clear()

        return result

    except litellm.exceptions.Timeout as e:
        # Upstream LLM provider socket deadline (httpx/LiteLLM). Distinct
        # 'socket_timeout' label lets dashboards tell "provider unhealthy"
        # from "asyncio scheduling stall" — both used to share the
        # status='timeout' bucket. The legacy 'timeout' bucket is kept for
        # back-compat with existing alerts.
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='socket_timeout').inc()
        LLM_CALLS_TOTAL.labels(status='timeout').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        logger.error(
            'LLM call socket timeout after %.3fs (httpx/LiteLLM-level, operation=%s): %s',
            elapsed,
            operation_name,
            e,
        )
        raise RuntimeError(
            f'LLM call timed out (upstream provider) after {elapsed:.3f}s ({operation_name})'
        ) from e

    except TimeoutError:
        # asyncio.wait_for fired before the provider surfaced a socket
        # timeout — Python-side stall (CPU-bound predictor body, scheduling
        # starvation, provider swallowing its own deadline).
        # Catch ordering depends on litellm.exceptions.Timeout NOT being a
        # subclass of TimeoutError (it derives from openai.APITimeoutError
        # → openai.APIError → Exception as of litellm 1.80.x). If a future
        # release restructures that hierarchy, both blocks must be revisited.
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='deadline_exceeded').inc()
        LLM_CALLS_TOTAL.labels(status='timeout').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        logger.error(
            'LLM call deadline exceeded after %ds (asyncio.wait_for, operation=%s)',
            timeout,
            operation_name,
        )
        raise RuntimeError(
            f'LLM call timed out (asyncio.wait_for) after {timeout}s ({operation_name})'
        ) from None

    except (ValueError, RuntimeError, OSError, KeyError) as e:
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='error').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        logger.error(f'DSPy operation failed: {e}')
        raise
