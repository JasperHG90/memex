"""
Circuit breaker for LLM calls.

Prevents hammering a failing LLM provider by tracking consecutive failures
and temporarily rejecting calls when the failure threshold is exceeded.

State machine:
  CLOSED (healthy)
    -> OPEN  (after failure_threshold consecutive failures)
    -> HALF_OPEN (after reset_timeout_seconds, allows one probe)
    -> CLOSED (if probe succeeds) or OPEN (if probe fails)

"""

import asyncio
import logging
import time
from enum import StrEnum
from typing import Any

from memex_common.config import CircuitBreakerConfig

logger = logging.getLogger('memex.core.circuit_breaker')


class CircuitState(StrEnum):
    """Circuit breaker states."""

    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half-open'


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, time_until_reset: float) -> None:
        self.time_until_reset = time_until_reset
        super().__init__(f'Circuit breaker is open. Retry in {time_until_reset:.1f}s.')


class CircuitBreaker:
    """
    Async-safe circuit breaker for LLM operations.

    Thread-safe via an asyncio.Lock guarding state transitions.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def pre_call(self) -> None:
        """
        Check the circuit state before making a call.

        Raises CircuitBreakerOpen if the circuit is open and the reset
        timeout has not yet elapsed.

        Transitions OPEN -> HALF_OPEN when the timeout has elapsed.
        """
        if not self._config.enabled:
            return

        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self._config.reset_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    logger.info('Circuit breaker transitioning OPEN -> HALF_OPEN (probe allowed)')
                else:
                    remaining = self._config.reset_timeout_seconds - elapsed
                    raise CircuitBreakerOpen(remaining)

    async def record_success(self) -> None:
        """Record a successful call. Resets the breaker to CLOSED."""
        if not self._config.enabled:
            return

        async with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info(f'Circuit breaker transitioning {self._state} -> CLOSED (success)')
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        """
        Record a failed call. Opens the circuit after failure_threshold
        consecutive failures.
        """
        if not self._config.enabled:
            return

        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed; re-open immediately
                self._state = CircuitState.OPEN
                logger.warning('Circuit breaker probe failed, re-opening circuit')
            elif self._failure_count >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f'Circuit breaker opened after {self._failure_count} consecutive failures'
                )

    async def __call__(
        self,
        coro_func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute an async callable through the circuit breaker.

        Usage:
            result = await breaker(some_async_func, arg1, arg2, kwarg=val)
        """
        await self.pre_call()
        try:
            result = await coro_func(*args, **kwargs)
            await self.record_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception:
            await self.record_failure()
            raise

    def reset(self) -> None:
        """Reset the circuit breaker to its initial CLOSED state (for testing)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
