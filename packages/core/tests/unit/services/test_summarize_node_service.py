"""Unit tests for ReflectionService.summarize_node (F5 service-layer).

Covers TC4 (rate-limit error path) at the unit level. TC2/TC3/TC2.5
remain in integration tests against a real DB. The unit test asserts
the service produces a service-layer ``RateLimitExceededError`` (NOT a
fastapi HTTPException) per RFC-002 alt-D rejection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.rate_limit import (
    RateLimitExceededError,
    TokenBucketRateLimiter,
)


@pytest.fixture
def reflection_service(mock_metastore, mock_config):
    """Build a ReflectionService with the rate limiter forced ON.

    The default ``mock_config`` disables the limiter for tests that don't
    care about throttling. Tests that DO care (TC4) toggle it on by
    swapping in a real, enabled bucket on the constructed service.
    """
    from memex_core.services.reflection import ReflectionService

    svc = ReflectionService(
        metastore=mock_metastore,
        config=mock_config,
        lm=MagicMock(),
        memory=MagicMock(),
        extraction=MagicMock(),
        queue_service=MagicMock(),
        embedding_model=MagicMock(),
    )
    return svc


@pytest.mark.asyncio
async def test_summarize_node_passes_through_when_rate_limit_disabled(reflection_service):
    """TC sanity: with the limiter disabled (default), summarize_node delegates to reflect."""
    expected = MagicMock()
    reflection_service.reflect = AsyncMock(return_value=expected)
    entity_id = uuid4()
    result = await reflection_service.summarize_node(entity_id, scope='incremental')
    assert result is expected
    reflection_service.reflect.assert_awaited_once()
    assert reflection_service.reflect.await_args is not None
    request_arg = reflection_service.reflect.await_args.args[0]
    assert request_arg.entity_id == entity_id
    assert request_arg.limit_recent_memories == 20


@pytest.mark.asyncio
async def test_summarize_node_full_scope_passes_none_limit(reflection_service):
    """scope='full' translates to limit_recent_memories=None on the request."""
    reflection_service.reflect = AsyncMock(return_value=MagicMock())
    entity_id = uuid4()
    await reflection_service.summarize_node(entity_id, scope='full')
    assert reflection_service.reflect.await_args is not None
    request_arg = reflection_service.reflect.await_args.args[0]
    assert request_arg.limit_recent_memories is None


@pytest.mark.asyncio
async def test_summarize_node_rate_limit_exceeded_raises_with_retry_after(reflection_service):
    """TC4: second back-to-back call raises RateLimitExceededError with retry_after_seconds."""
    reflection_service.reflect = AsyncMock(return_value=MagicMock())

    # Swap in an enabled limiter that will reject the second call.
    reflection_service._summarize_node_limiter = TokenBucketRateLimiter(
        per_seconds=60.0, burst=1, enabled=True
    )

    entity_id = uuid4()
    vault_id = uuid4()
    await reflection_service.summarize_node(entity_id, scope='incremental', vault_id=vault_id)

    with pytest.raises(RateLimitExceededError) as exc:
        await reflection_service.summarize_node(entity_id, scope='incremental', vault_id=vault_id)
    assert 0 < exc.value.retry_after_seconds <= 60.0
    assert exc.value.key == (entity_id, vault_id)
    assert reflection_service.reflect.await_count == 1


@pytest.mark.asyncio
async def test_summarize_node_rate_limit_keys_per_entity_and_vault(reflection_service):
    """Two distinct (entity, vault) keys do not throttle each other."""
    reflection_service.reflect = AsyncMock(return_value=MagicMock())
    reflection_service._summarize_node_limiter = TokenBucketRateLimiter(
        per_seconds=60.0, burst=1, enabled=True
    )

    eid_a, eid_b = uuid4(), uuid4()
    vault = uuid4()
    await reflection_service.summarize_node(eid_a, scope='incremental', vault_id=vault)
    await reflection_service.summarize_node(eid_b, scope='incremental', vault_id=vault)
    assert reflection_service.reflect.await_count == 2
