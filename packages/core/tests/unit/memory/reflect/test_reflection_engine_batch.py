import pytest
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_core.config import MemexConfig


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    session.exec = AsyncMock()
    return session


@pytest.fixture
def engine(mock_session):
    mock_config = MagicMock(spec=MemexConfig)
    return ReflectionEngine(session=mock_session, config=mock_config, embedder=MagicMock())


@pytest.mark.asyncio
async def test_batch_fetch_recent_memories_sql_structure(engine, mock_session):
    """
    Verify that _batch_fetch_recent_memories constructs a valid query
    and handles the result grouping correctly.
    """
    entity_ids = [uuid4(), uuid4()]

    # Mock the DB response
    # Format: [(MemoryUnit, entity_id), ...]
    unit1 = MemoryUnit(text='U1')
    unit2 = MemoryUnit(text='U2')

    mock_result = MagicMock()
    mock_result.all.return_value = [
        (unit1, entity_ids[0]),
        (unit2, entity_ids[0]),
        (unit1, entity_ids[1]),  # Shared unit case
    ]
    mock_session.exec.return_value = mock_result

    # Execute
    result_map = await engine._batch_fetch_recent_memories(entity_ids, limit_per_entity=5)

    # Verify Grouping
    assert len(result_map[entity_ids[0]]) == 2
    assert len(result_map[entity_ids[1]]) == 1

    # Verify SQL execution happened
    mock_session.exec.assert_called_once()

    # Inspect the call args to sanity check logic (hard to verify exact SQL string with mocks,
    # but we check if it didn't crash during construction)


@pytest.mark.asyncio
async def test_batch_get_or_create_models_logic(engine, mock_session):
    """Test mixed existing and new models."""
    existing_id = uuid4()
    missing_id = uuid4()

    # Mock existing finding
    mock_existing_model = MagicMock()
    mock_existing_model.entity_id = existing_id

    mock_session.exec.side_effect = [
        # 1. Query for models
        MagicMock(all=MagicMock(return_value=[mock_existing_model])),
        # 2. Query for missing entities (names)
        MagicMock(
            all=MagicMock(return_value=[MagicMock(id=missing_id, canonical_name='New Entity')])
        ),
    ]

    models_map = await engine._batch_get_or_create_models([existing_id, missing_id])

    assert len(models_map) == 2
    assert models_map[existing_id] == mock_existing_model
    assert models_map[missing_id].name == 'New Entity'

    # Verify new model was added
    mock_session.add.assert_called()
