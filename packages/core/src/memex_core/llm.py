import logging
import asyncio
from typing import Any, TypeVar

import dspy
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.circuit_breaker import CircuitBreaker
from memex_core.context import get_session_id
from memex_core.memory.sql_models import TokenUsage

logger = logging.getLogger('memex.core.llm')

T = TypeVar('T')

# Module-level circuit breaker instance shared across all LLM calls.
# Initialised with default config; call configure_circuit_breaker() to
# override from the application's CircuitBreakerConfig.
_circuit_breaker = CircuitBreaker()


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
    session: AsyncSession | None = None,
    context_metadata: dict | None = None,
    semaphore: asyncio.Semaphore | None = None,
    vault_id: Any | None = None,
) -> tuple[Any, TokenUsage]:
    """
    Executes a DSPy predictor, extracts token usage, and optionally logs it to DB.

    This wrapper isolates the LM history by copying the LM object, ensuring that
    token usage can be accurately extracted for a specific call even in concurrent environments.

    Args:
        lm: The DSPy LM instance to use.
        predictor: The configured DSPy predictor (or ChainOfThought/Program).
        input_kwargs: Dictionary of arguments to pass to the predictor.
        session: SQLModel AsyncSession. Required if usage logging is desired.
        context_metadata: Additional metadata for the log (e.g. {'operation': 'extract'}).
        semaphore: Optional semaphore for concurrency control.
        vault_id: Optional UUID of the vault to associate with the usage log.

    Returns:
        tuple(result, TokenUsage)
    """

    # Check circuit breaker before attempting the LLM call
    await _circuit_breaker.pre_call()

    # Shallow copy to isolate history for this specific call
    lm_ = lm.copy()

    async def _execute():
        with dspy.context(lm=lm_):
            # We use acall if available, or fallback to the predictor itself
            if hasattr(predictor, 'acall'):
                return await predictor.acall(**input_kwargs)
            else:
                # Predictors in older dspy might not have acall
                return await asyncio.to_thread(predictor, **input_kwargs)

    try:
        if semaphore:
            async with semaphore:
                result = await _execute()
        else:
            result = await _execute()

        # Record success with circuit breaker
        await _circuit_breaker.record_success()

        # Extract Usage
        token_usage = TokenUsage()
        model_name = lm_.model
        is_cached = False

        if lm_.history:
            last_run = lm_.history[-1]
            usage_data = last_run.get('usage', {})

            resp_obj = last_run.get('response')
            if resp_obj and hasattr(resp_obj, 'cache_hit') and resp_obj.cache_hit:
                is_cached = True

            # Handle different adapter formats for usage
            # Preserve None if stats are not reported
            token_usage = TokenUsage(
                input_tokens=usage_data.get('prompt_tokens'),
                output_tokens=usage_data.get('completion_tokens'),
                total_tokens=usage_data.get('total_tokens'),
                is_cached=is_cached,
                models=[model_name] if model_name else [],
                cost=usage_data.get('cost'),
            )

            if vault_id:
                token_usage.vault_id = vault_id

            # Log to DB if session is provided and requested
            if session is not None:
                try:
                    # Reuse the metadata that includes the 'cached' flag
                    db_metadata = context_metadata or {}
                    if is_cached and 'cached' not in db_metadata:
                        db_metadata = db_metadata.copy()
                        db_metadata['cached'] = True

                    token_usage.session_id = get_session_id()
                    token_usage.context_metadata = db_metadata

                    session.add(token_usage)
                    # Note: The caller is expected to commit the session later.
                except Exception as db_err:
                    logger.warning(f'Failed to stage token usage log: {db_err}')

        # Clear LM history to prevent memory accumulation
        if hasattr(lm_, 'history'):
            lm_.history.clear()

        return result, token_usage

    except Exception as e:
        # Record failure with circuit breaker
        await _circuit_breaker.record_failure()
        logger.error(f'DSPy operation failed: {e}')
        raise
