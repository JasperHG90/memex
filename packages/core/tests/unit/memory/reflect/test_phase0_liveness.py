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


@pytest.mark.asyncio
async def test_phase_0_preserves_naturally_empty_evidence_observations():
    """Observations that legitimately have no evidence should not be dropped by liveness check."""
    dead_id = uuid4()
    entity_id = uuid4()

    obs_with_dead = Observation(
        title='Has dead evidence',
        content='Will be pruned to empty',
        evidence=[
            EvidenceItem(memory_id=dead_id, quote='dead', relevance=1.0),
        ],
    )
    obs_naturally_empty = Observation(
        title='Naturally empty',
        content='Never had evidence',
        evidence=[],
    )

    model = MentalModel(
        entity_id=entity_id,
        name='Test Entity',
        observations=[
            obs_with_dead.model_dump(mode='json'),
            obs_naturally_empty.model_dump(mode='json'),
        ],
    )

    engine = _make_engine()

    # Mock: dead_id is not live
    async def mock_exec(stmt):
        result = MagicMock()
        result.all.return_value = []
        return result

    engine.session.exec = AsyncMock(side_effect=mock_exec)

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, None)
        result = await engine._phase_0_update(model, 'Test Entity', [])

    # obs_with_dead should be pruned away, but obs_naturally_empty should survive
    assert len(result) == 1
    assert result[0].title == 'Naturally empty'


@pytest.mark.asyncio
async def test_phase_0_prunes_out_of_vault_evidence():
    """AC-004: Evidence citing a unit in a *different* vault is treated as dead."""
    vault_a = uuid4()
    unit_in_vault_b = uuid4()

    obs = Observation(
        title='Cross-vault evidence',
        content='Evidence points to a unit in vault B',
        evidence=[
            EvidenceItem(memory_id=unit_in_vault_b, quote='from vault B', relevance=1.0),
        ],
    )

    model = MentalModel(
        entity_id=uuid4(),
        name='Test Entity',
        observations=[obs.model_dump(mode='json')],
    )

    engine = _make_engine()

    # The liveness query now filters by vault_id. The unit exists in the DB
    # but in vault_b, so the vault-filtered query returns nothing.
    captured_stmts: list = []

    async def mock_exec(stmt):
        captured_stmts.append(stmt)
        result = MagicMock()
        # Return empty: the unit exists but not in vault_a or GLOBAL
        result.all.return_value = []
        return result

    engine.session.exec = AsyncMock(side_effect=mock_exec)

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, None)
        result = await engine._phase_0_update(model, 'Test Entity', [], vault_id=vault_a)

    # The unit from vault B should be treated as dead -> observation pruned
    assert len(result) == 0

    # Verify the liveness query was issued with vault filtering
    assert len(captured_stmts) == 1
    compiled = captured_stmts[0].compile(compile_kwargs={'literal_binds': True})
    sql_text = str(compiled)
    assert 'vault_id' in sql_text, 'Liveness query must filter by vault_id'


@pytest.mark.asyncio
async def test_phase_0_keeps_global_vault_evidence():
    """AC-005: Evidence citing a unit in GLOBAL_VAULT_ID passes the liveness check."""
    vault_a = uuid4()
    global_unit_id = uuid4()

    obs = Observation(
        title='Global vault evidence',
        content='Evidence points to a unit in the global vault',
        evidence=[
            EvidenceItem(memory_id=global_unit_id, quote='global fact', relevance=1.0),
        ],
    )

    model = MentalModel(
        entity_id=uuid4(),
        name='Test Entity',
        observations=[obs.model_dump(mode='json')],
    )

    engine = _make_engine()

    # The unit is in GLOBAL_VAULT_ID — the vault-filtered query should return it
    async def mock_exec(stmt):
        result = MagicMock()
        # Unit is in global vault, so it passes the filter
        result.all.return_value = [global_unit_id]
        return result

    engine.session.exec = AsyncMock(side_effect=mock_exec)

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, None)
        result = await engine._phase_0_update(model, 'Test Entity', [], vault_id=vault_a)

    # Evidence should survive — global vault units are always live
    assert len(result) == 1
    assert len(result[0].evidence) == 1
    assert result[0].evidence[0].memory_id == global_unit_id
