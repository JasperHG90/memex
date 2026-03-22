import logging
import asyncio
import time
from typing import Any, TypeVar

import dspy

from memex_core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from memex_core.metrics import (
    LLM_CALLS_TOTAL,
    LLM_CALL_DURATION_SECONDS,
    CIRCUIT_BREAKER_REJECTIONS_TOTAL,
    CIRCUIT_BREAKER_STATE,
)

logger = logging.getLogger('memex.core.llm')

T = TypeVar('T')

# Module-level circuit breaker instance shared across all LLM calls.
# Initialised with default config; call configure_circuit_breaker() to
# override from the application's CircuitBreakerConfig.
_circuit_breaker = CircuitBreaker()

# Circuit breaker state encoding for Prometheus gauge
_STATE_VALUES = {'closed': 0, 'open': 1, 'half-open': 2}


_llm_timeout_seconds: float = 120.0


def configure_llm_timeout(seconds: float) -> None:
    """Set the per-call LLM timeout (called during app startup)."""
    global _llm_timeout_seconds
    _llm_timeout_seconds = seconds


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
        with dspy.context(lm=lm_):
            if hasattr(predictor, 'acall'):
                return await predictor.acall(**input_kwargs)
            else:
                return await asyncio.to_thread(predictor, **input_kwargs)

    try:
        if semaphore:
            async with semaphore:
                result = await asyncio.wait_for(_execute(), timeout=_llm_timeout_seconds)
        else:
            result = await asyncio.wait_for(_execute(), timeout=_llm_timeout_seconds)

        await _circuit_breaker.record_success()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='success').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        # Clear LM history to prevent memory accumulation
        if hasattr(lm_, 'history'):
            lm_.history.clear()

        return result

    except asyncio.TimeoutError:
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='timeout').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        logger.error(f'LLM call timed out after {_llm_timeout_seconds}s')
        raise TimeoutError(
            f'LLM inference timed out after {_llm_timeout_seconds}s'
        )

    except (ValueError, RuntimeError, OSError, KeyError) as e:
        await _circuit_breaker.record_failure()

        elapsed = time.monotonic() - start
        LLM_CALLS_TOTAL.labels(status='error').inc()
        LLM_CALL_DURATION_SECONDS.observe(elapsed)
        CIRCUIT_BREAKER_STATE.set(_STATE_VALUES.get(str(_circuit_breaker.state), 0))

        logger.error(f'DSPy operation failed: {e}')
        raise
