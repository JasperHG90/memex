"""Tests for _phase_5_finalize — CAS UPDATE behavior + entity_metadata population."""

import pytest
import numpy as np
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import MentalModel, Observation
from memex_core.config import MemexConfig


def _make_engine(rowcount: int = 1) -> ReflectionEngine:
    mock_session = AsyncMock()
    cas_result = MagicMock()
    cas_result.rowcount = rowcount
    mock_session.execute = AsyncMock(return_value=cas_result)
    mock_config = MagicMock(spec=MemexConfig)
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = np.array([[0.1] * 384])
    return ReflectionEngine(session=mock_session, config=mock_config, embedder=mock_embedder)


@pytest.fixture
def engine():
    return _make_engine(rowcount=1)


@pytest.mark.asyncio
async def test_phase_5_populates_entity_metadata(engine):
    """On CAS success, entity_metadata is populated and version increments."""
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

    applied = await engine._phase_5_finalize(
        model,
        obs,
        entity_summary='A test entity for unit testing.',
        entity_type='person',
    )

    assert applied is True
    assert model.entity_metadata == {
        'description': 'A test entity for unit testing.',
        'category': 'person',
        'observation_count': 2,
    }
    assert model.version == 1


@pytest.mark.asyncio
async def test_phase_5_with_empty_summary_and_none_type(engine):
    """Empty summary and None entity_type are accepted and reflected in metadata."""
    model = MentalModel(
        id=uuid4(),
        entity_id=uuid4(),
        vault_id=uuid4(),
        name='Test Entity',
        observations=[],
        version=0,
    )

    applied = await engine._phase_5_finalize(
        model,
        [],
        entity_summary='',
        entity_type=None,
    )

    assert applied is True
    assert model.entity_metadata == {
        'description': '',
        'category': None,
        'observation_count': 0,
    }


@pytest.mark.asyncio
async def test_phase_5_preserves_prior_description_when_summary_empty(engine):
    """An empty incoming summary must NOT blank an existing description.

    Regression: a no-new-observation reflection cycle yields entity_summary=''
    (Phase 4 short-circuit). Without this guard Phase 5 overwrote the
    description built by the last full cycle, leaving entities with many
    observations but a blank description (the user-reported MCP/CLI bug).
    """
    model = MentalModel(
        id=uuid4(),
        entity_id=uuid4(),
        vault_id=uuid4(),
        name='MCP',
        observations=[],
        version=0,
        entity_metadata={
            'description': 'The Model Context Protocol.',
            'category': 'Technology',
            'observation_count': 14,
        },
    )

    applied = await engine._phase_5_finalize(
        model,
        [],
        entity_summary='',
        entity_type='Technology',
    )

    assert applied is True
    # Prior description preserved rather than clobbered with ''.
    assert model.entity_metadata['description'] == 'The Model Context Protocol.'


class TestCASBehavior:
    @pytest.mark.asyncio
    async def test_cas_success_returns_true_and_mutates_model(self):
        """CAS UPDATE rowcount=1 → returns True, in-memory model reflects new state."""
        engine = _make_engine(rowcount=1)
        model = MentalModel(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
            name='Test',
            observations=[],
            version=7,
        )
        obs = [Observation(title='O', content='C', evidence=[])]
        applied = await engine._phase_5_finalize(model, obs, entity_summary='s', entity_type='t')
        assert applied is True
        assert model.version == 8
        assert len(model.observations) == 1

    @pytest.mark.asyncio
    async def test_cas_abandon_returns_false_and_preserves_model(self):
        """CAS UPDATE rowcount=0 → returns False, in-memory model is NOT mutated."""
        engine = _make_engine(rowcount=0)
        model = MentalModel(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
            name='Test',
            observations=[{'title': 'Pre', 'content': 'Pre', 'evidence': []}],
            version=7,
            entity_metadata={
                'description': 'unchanged',
                'category': 'orig',
                'observation_count': 1,
            },
        )
        obs = [
            Observation(title='New', content='New', evidence=[]),
            Observation(title='New2', content='New2', evidence=[]),
        ]
        applied = await engine._phase_5_finalize(model, obs, entity_summary='new', entity_type='t')
        assert applied is False
        assert model.version == 7
        assert model.observations == [{'title': 'Pre', 'content': 'Pre', 'evidence': []}]
        assert model.entity_metadata == {
            'description': 'unchanged',
            'category': 'orig',
            'observation_count': 1,
        }

    @pytest.mark.asyncio
    async def test_cas_statement_targets_id_and_claimed_version(self):
        """The UPDATE WHERE clause references id AND the claimed version."""
        engine = _make_engine(rowcount=1)
        model = MentalModel(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
            name='Test',
            observations=[],
            version=3,
        )
        await engine._phase_5_finalize(model, [], entity_summary='', entity_type=None)
        assert engine.session.execute.call_count == 1
        stmt = engine.session.execute.call_args.args[0]
        # Render the WHERE clause (which has no JSONB literals) for inspection
        where_sql = str(stmt.whereclause.compile(compile_kwargs={'literal_binds': True}))
        assert 'version' in where_sql
        assert '= 3' in where_sql.replace(' ', ' ')
        assert 'mental_models.id' in where_sql
        # Confirm the bumped-version path in the SET values
        version_set = stmt._values[stmt.table.c.version]
        version_set_sql = str(version_set.compile(compile_kwargs={'literal_binds': True}))
        assert '+ 1' in version_set_sql or 'version + 1' in version_set_sql

    @pytest.mark.asyncio
    async def test_cas_abandon_does_not_raise(self):
        """Abandon path is a clean return path, not an exception."""
        engine = _make_engine(rowcount=0)
        model = MentalModel(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
            name='Test',
            observations=[],
            version=0,
        )
        # Should not raise
        applied = await engine._phase_5_finalize(model, [], entity_summary='', entity_type=None)
        assert applied is False

    @pytest.mark.asyncio
    async def test_cas_missing_rowcount_treated_as_abandon(self):
        """Result objects without a rowcount attribute (rare backend quirk) default to abandon."""
        mock_session = AsyncMock()
        cas_result = MagicMock(spec=[])  # no rowcount attribute
        mock_session.execute = AsyncMock(return_value=cas_result)
        mock_config = MagicMock(spec=MemexConfig)
        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = np.array([[0.1] * 384])
        engine = ReflectionEngine(session=mock_session, config=mock_config, embedder=mock_embedder)

        model = MentalModel(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
            name='Test',
            observations=[],
            version=0,
        )
        applied = await engine._phase_5_finalize(model, [], entity_summary='', entity_type=None)
        assert applied is False
