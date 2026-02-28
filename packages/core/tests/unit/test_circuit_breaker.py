"""Tests for the circuit breaker module."""

import asyncio

import pytest

from memex_common.config import CircuitBreakerConfig
from memex_core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
)


@pytest.fixture
def default_breaker() -> CircuitBreaker:
    return CircuitBreaker()


@pytest.fixture
def fast_breaker() -> CircuitBreaker:
    """Breaker with low threshold and short timeout for fast tests."""
    return CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=0.1))


@pytest.fixture
def disabled_breaker() -> CircuitBreaker:
    return CircuitBreaker(CircuitBreakerConfig(enabled=False))


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_starts_closed(self, default_breaker: CircuitBreaker) -> None:
        assert default_breaker.state == CircuitState.CLOSED

    def test_starts_with_zero_failures(self, default_breaker: CircuitBreaker) -> None:
        assert default_breaker.failure_count == 0


# ---------------------------------------------------------------------------
# CLOSED -> OPEN transition
# ---------------------------------------------------------------------------


class TestClosedToOpen:
    @pytest.mark.asyncio
    async def test_opens_after_threshold(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        assert fast_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_stays_closed_below_threshold(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(2):
            await fast_breaker.record_failure()
        assert fast_breaker.state == CircuitState.CLOSED
        assert fast_breaker.failure_count == 2

    @pytest.mark.asyncio
    async def test_pre_call_rejects_when_open(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await fast_breaker.pre_call()
        assert exc_info.value.time_until_reset > 0


# ---------------------------------------------------------------------------
# OPEN -> HALF_OPEN transition
# ---------------------------------------------------------------------------


class TestOpenToHalfOpen:
    @pytest.mark.asyncio
    async def test_transitions_after_timeout(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        assert fast_breaker.state == CircuitState.OPEN

        # Wait for the reset timeout to elapse
        await asyncio.sleep(0.15)

        # pre_call should transition to HALF_OPEN and NOT raise
        await fast_breaker.pre_call()
        assert fast_breaker.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# HALF_OPEN -> CLOSED (probe success)
# ---------------------------------------------------------------------------


class TestHalfOpenToClosed:
    @pytest.mark.asyncio
    async def test_success_closes_circuit(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        await asyncio.sleep(0.15)
        await fast_breaker.pre_call()  # -> HALF_OPEN
        assert fast_breaker.state == CircuitState.HALF_OPEN

        await fast_breaker.record_success()
        assert fast_breaker.state == CircuitState.CLOSED
        assert fast_breaker.failure_count == 0


# ---------------------------------------------------------------------------
# HALF_OPEN -> OPEN (probe failure)
# ---------------------------------------------------------------------------


class TestHalfOpenToOpen:
    @pytest.mark.asyncio
    async def test_failure_reopens_circuit(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        await asyncio.sleep(0.15)
        await fast_breaker.pre_call()  # -> HALF_OPEN
        assert fast_breaker.state == CircuitState.HALF_OPEN

        await fast_breaker.record_failure()
        assert fast_breaker.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Success resets failure count
# ---------------------------------------------------------------------------


class TestSuccessReset:
    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, fast_breaker: CircuitBreaker) -> None:
        await fast_breaker.record_failure()
        await fast_breaker.record_failure()
        assert fast_breaker.failure_count == 2

        await fast_breaker.record_success()
        assert fast_breaker.failure_count == 0
        assert fast_breaker.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# __call__ wrapper
# ---------------------------------------------------------------------------


class TestCallWrapper:
    @pytest.mark.asyncio
    async def test_successful_call(self, fast_breaker: CircuitBreaker) -> None:
        async def succeed() -> str:
            return 'ok'

        result = await fast_breaker(succeed)
        assert result == 'ok'
        assert fast_breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failing_call_records_failure(self, fast_breaker: CircuitBreaker) -> None:
        async def fail() -> None:
            raise RuntimeError('boom')

        with pytest.raises(RuntimeError, match='boom'):
            await fast_breaker(fail)
        assert fast_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_call_rejected_when_open(self, fast_breaker: CircuitBreaker) -> None:
        async def fail() -> None:
            raise RuntimeError('boom')

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await fast_breaker(fail)

        assert fast_breaker.state == CircuitState.OPEN

        # Next call should be rejected without invoking the function
        call_count = 0

        async def should_not_run() -> str:
            nonlocal call_count
            call_count += 1
            return 'unreachable'

        with pytest.raises(CircuitBreakerOpen):
            await fast_breaker(should_not_run)
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_call_passes_args_and_kwargs(self, fast_breaker: CircuitBreaker) -> None:
        async def add(a: int, b: int, extra: int = 0) -> int:
            return a + b + extra

        result = await fast_breaker(add, 1, 2, extra=10)
        assert result == 13


# ---------------------------------------------------------------------------
# Disabled breaker
# ---------------------------------------------------------------------------


class TestDisabledBreaker:
    @pytest.mark.asyncio
    async def test_pre_call_does_nothing(self, disabled_breaker: CircuitBreaker) -> None:
        # Even after many failures, pre_call should not raise
        for _ in range(100):
            await disabled_breaker.record_failure()
        await disabled_breaker.pre_call()  # should not raise

    @pytest.mark.asyncio
    async def test_call_works_normally(self, disabled_breaker: CircuitBreaker) -> None:
        async def succeed() -> str:
            return 'ok'

        result = await disabled_breaker(succeed)
        assert result == 'ok'


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_returns_to_closed(self, fast_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await fast_breaker.record_failure()
        assert fast_breaker.state == CircuitState.OPEN

        fast_breaker.reset()
        assert fast_breaker.state == CircuitState.CLOSED
        assert fast_breaker.failure_count == 0


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def test_default_config(self) -> None:
        config = CircuitBreakerConfig()
        assert config.enabled is True
        assert config.failure_threshold == 5
        assert config.reset_timeout_seconds == 60.0

    def test_custom_config(self) -> None:
        config = CircuitBreakerConfig(
            failure_threshold=10,
            reset_timeout_seconds=120.0,
        )
        assert config.failure_threshold == 10
        assert config.reset_timeout_seconds == 120.0

    def test_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_threshold=0)

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreakerConfig(reset_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Concurrency safety
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_failures_reach_threshold(self) -> None:
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=5, reset_timeout_seconds=1.0)
        )

        async def record_one_failure() -> None:
            await breaker.record_failure()

        # Fire 5 failures concurrently
        await asyncio.gather(*[record_one_failure() for _ in range(5)])

        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 5
