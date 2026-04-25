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

# Module-level circuit breaker. ``run_dspy_operation`` resolves
# ``_circuit_breaker`` *at call time* (not by capturing a closure), so a
# call to ``configure_circuit_breaker(...)`` at app startup is observed
# by every subsequent LLM call. Importers that bind ``_circuit_breaker``
# into a local name at import time (``from memex_core.llm import
# _circuit_breaker``) would see a stale reference; use
# ``get_circuit_breaker()`` instead. F20 (Phase 3 review) tightened
# this comment to make the late-binding contract explicit.
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
        # F10: catch the upstream-provider timeout family inside _execute so the
        # outer handler can tell "provider socket deadline fired" (httpx/LiteLLM
        # at the network layer — POC-001 path) from "asyncio.wait_for fired
        # around _execute" (Python-side stall, e.g. circuit breaker, scheduling
        # starvation). The inner branch logs the actionable provider error;
        # the outer branch logs the asyncio-level deadline that elapsed.
        if _tracer is not None and _oi_using_attributes is not None:
            with _tracer.start_as_current_span(operation_name, kind=_SpanKind.INTERNAL):
                with _oi_using_attributes(metadata={'memex.stage': operation_name}):
                    with dspy.context(lm=lm_):
                        if hasattr(predictor, 'acall'):
                            return await predictor.acall(**input_kwargs)
                        else:
                            # dead path: DSPy 3.1+ always exposes acall (AC-009 four-bucket audit)
                            return await asyncio.to_thread(predictor, **input_kwargs)
        else:
            with dspy.context(lm=lm_):
                if hasattr(predictor, 'acall'):
                    return await predictor.acall(**input_kwargs)
                else:
                    # dead path: DSPy 3.1+ always exposes acall (AC-009 four-bucket audit)
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
        # Inner: upstream LLM provider socket deadline fired (httpx/LiteLLM).
        # POC-001 confirmed this is what bubbles up when dspy.LM(timeout=N)
        # plumbs through to the underlying httpx client. Distinct log line +
        # metric label ('socket_timeout') so operators can tell "provider
        # unhealthy" from "asyncio scheduling stall" — both used to share the
        # status='timeout' bucket pre-F10.
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        # F10: distinct label for the provider-side socket-timeout branch.
        LLM_CALLS_TOTAL.labels(status='socket_timeout').inc()
        # Back-compat: also emit the legacy 'timeout' bucket so existing
        # dashboards/alerts keep firing on a sum across both new labels.
        # Per PO recommendation in #41 (Phase 3 review), retain for at least
        # one release; revisit removal as a follow-up.
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
        # Outer: asyncio.wait_for(timeout=...) fired before _execute returned.
        # Means the provider didn't surface a socket timeout in time — the
        # Python-side deadline tripped first (e.g. coroutine scheduling stall,
        # an event-loop stuck in a CPU-bound predictor body, or a provider
        # that swallows its own timeout). Distinct label ('deadline_exceeded')
        # plus the back-compat 'timeout' bucket; see inner branch for rationale.
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        # F10: distinct label for the asyncio.wait_for-deadline branch.
        LLM_CALLS_TOTAL.labels(status='deadline_exceeded').inc()
        # Back-compat: legacy 'timeout' bucket — see inner branch comment.
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
