"""Unit tests for Phase 0 stale evidence liveness check."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import (
    EvidenceItem,
    MentalModel,
    Observation,
)


def _make_engine() -> ReflectionEngine:
    mock_config = MagicMock()
    mock_config.server.memory.reflection.model.model = 'test-model'
    engine = ReflectionEngine(session=AsyncMock(), config=mock_config, embedder=MagicMock())
    engine.lm = MagicMock()
    return engine


@pytest.mark.asyncio
async def test_phase_0_prunes_stale_evidence():
    """Stale evidence is pruned, empty observations dropped, model.observations updated."""
    live_id = uuid4()
    dead_id = uuid4()
    entity_id = uuid4()

    obs_with_both = Observation(
        title='Mixed evidence',
        content='Has live and dead evidence',
        evidence=[
            EvidenceItem(memory_id=live_id, quote='live', relevance=1.0),
            EvidenceItem(memory_id=dead_id, quote='dead', relevance=1.0),
        ],
    )
    obs_only_dead = Observation(
        title='All dead',
        content='Only dead evidence',
        evidence=[
            EvidenceItem(memory_id=dead_id, quote='dead too', relevance=1.0),
        ],
    )

    model = MentalModel(
        entity_id=entity_id,
        name='Test Entity',
        observations=[
            obs_with_both.model_dump(mode='json'),
            obs_only_dead.model_dump(mode='json'),
        ],
    )

    engine = _make_engine()

    # Mock the session.exec to return only live_id as existing
    async def mock_exec(stmt):
        result = MagicMock()
        result.all.return_value = [live_id]
        return result

    engine.session.exec = AsyncMock(side_effect=mock_exec)

    # No new memories — phase 0 should still do the liveness check
    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, None)
        result = await engine._phase_0_update(model, 'Test Entity', [])

    # obs_only_dead should be dropped entirely, obs_with_both should have 1 evidence
    assert len(result) == 1
    assert result[0].title == 'Mixed evidence'
    assert len(result[0].evidence) == 1
    assert result[0].evidence[0].memory_id == live_id

    # model.observations should be updated (flag_modified was called internally)
    assert len(model.observations) == 1


@pytest.mark.asyncio
async def test_phase_0_preserves_live_evidence():
    """When all evidence is live, no pruning occurs and observations are unchanged."""
    live_id_1 = uuid4()
    live_id_2 = uuid4()
    entity_id = uuid4()

    obs = Observation(
        title='All live',
        content='Both evidence items are live',
        evidence=[
            EvidenceItem(memory_id=live_id_1, quote='q1', relevance=1.0),
            EvidenceItem(memory_id=live_id_2, quote='q2', relevance=1.0),
        ],
    )

    original_obs_dict = obs.model_dump(mode='json')
    model = MentalModel(
        entity_id=entity_id,
        name='Test Entity',
        observations=[original_obs_dict],
    )

    engine = _make_engine()

    # Mock session.exec to return both IDs as live
    async def mock_exec(stmt):
        result = MagicMock()
        result.all.return_value = [live_id_1, live_id_2]
        return result

    engine.session.exec = AsyncMock(side_effect=mock_exec)

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, None)
        result = await engine._phase_0_update(model, 'Test Entity', [])

    assert len(result) == 1
    assert len(result[0].evidence) == 2
    # model.observations should be unchanged (same reference)
    assert model.observations == [original_obs_dict]
