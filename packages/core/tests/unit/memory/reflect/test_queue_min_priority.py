"""Tests that process_reflection_queue respects the min_priority config."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig, ReflectionConfig, ServerConfig, MemoryConfig
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.sql_models import ReflectionQueue, ReflectionStatus


VAULT_ID = uuid4()


def _make_engine(min_priority: float) -> MemoryEngine:
    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                reflection=ReflectionConfig(min_priority=min_priority),
            ),
        ),
    )
    extraction = MagicMock()
    extraction.embedding_model = MagicMock()
    retrieval = MagicMock()
    return MemoryEngine(
        config=config,
        extraction_engine=extraction,
        retrieval_engine=retrieval,
    )


@pytest.mark.asyncio
async def test_process_reflection_queue_skips_below_threshold():
    """Items below min_priority must not be selected."""
    engine = _make_engine(min_priority=0.51)
    session = AsyncMock(spec=AsyncSession)

    # Return empty result so we don't have to mock the reflection path
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result

    processed = await engine.process_reflection_queue(session, limit=10)
    assert processed == 0

    # Verify the SQL statement includes the priority filter
    stmt = session.exec.call_args[0][0]
    compiled = stmt.compile(compile_kwargs={'literal_binds': True})
    sql = str(compiled)
    assert 'priority_score' in sql
    assert '0.51' in sql


@pytest.mark.asyncio
async def test_process_reflection_queue_includes_failed_status():
    """The query must select both PENDING and FAILED items."""
    engine = _make_engine(min_priority=0.3)
    session = AsyncMock(spec=AsyncSession)

    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result

    await engine.process_reflection_queue(session, limit=10)

    stmt = session.exec.call_args[0][0]
    compiled = stmt.compile(compile_kwargs={'literal_binds': True})
    sql = str(compiled)
    assert 'pending' in sql
    assert 'failed' in sql


@pytest.mark.asyncio
async def test_process_reflection_queue_claims_above_threshold():
    """Items at or above min_priority should be processed."""
    engine = _make_engine(min_priority=0.5)
    session = AsyncMock(spec=AsyncSession)

    task = ReflectionQueue(
        entity_id=uuid4(),
        vault_id=VAULT_ID,
        priority_score=0.5,
        status=ReflectionStatus.PENDING,
        accumulated_evidence=1,
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [task]
    session.exec.return_value = mock_result

    # Mock the reflect_batch call so we don't need a real reflection engine
    mock_model = MagicMock()
    mock_model.entity_id = task.entity_id
    mock_model.vault_id = task.vault_id

    with patch('memex_core.memory.engine.ReflectionEngine') as mock_reflector_cls:
        mock_reflector = MagicMock()
        mock_reflector.reflect_batch = AsyncMock(return_value=[mock_model])
        mock_reflector_cls.return_value = mock_reflector

        processed = await engine.process_reflection_queue(session, limit=10)

    assert processed == 1
    assert task.status == ReflectionStatus.PROCESSING or session.delete.called
