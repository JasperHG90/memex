"""Tests for _phase_5_finalize — entity_metadata population."""

import asyncio
import pytest
import numpy as np
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import MentalModel, Observation
from memex_core.config import MemexConfig


@pytest.fixture
def engine():
    mock_session = AsyncMock()
    mock_config = MagicMock(spec=MemexConfig)
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = np.array([[0.1] * 384])
    return ReflectionEngine(session=mock_session, config=mock_config, embedder=mock_embedder)


@pytest.fixture
def db_lock():
    return asyncio.Lock()


@pytest.mark.asyncio
async def test_phase_5_populates_entity_metadata(engine, db_lock):
    """_phase_5_finalize should populate entity_metadata with description, category, count."""
    model = MentalModel(
        id=uuid4(),
        entity_id=uuid4(),
        vault_id=uuid4(),
        name='Test Entity',
        observations=[],
        version=0,
    )

    obs = [
        Observation(title='Obs 1', content='Content 1', evidence=[]),
        Observation(title='Obs 2', content='Content 2', evidence=[]),
    ]

    await engine._phase_5_finalize(
        model,
        obs,
        db_lock,
        entity_summary='A test entity for unit testing.',
        entity_type='person',
    )

    assert model.entity_metadata == {
        'description': 'A test entity for unit testing.',
        'category': 'person',
        'observation_count': 2,
    }
    assert model.version == 1


@pytest.mark.asyncio
async def test_phase_5_with_empty_summary_and_none_type(engine, db_lock):
    """_phase_5_finalize handles empty summary and None entity_type."""
    model = MentalModel(
        id=uuid4(),
        entity_id=uuid4(),
        vault_id=uuid4(),
        name='Test Entity',
        observations=[],
        version=0,
    )

    await engine._phase_5_finalize(
        model,
        [],
        db_lock,
        entity_summary='',
        entity_type=None,
    )

    assert model.entity_metadata == {
        'description': '',
        'category': None,
        'observation_count': 0,
    }
