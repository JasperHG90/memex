"""Unit tests for prune_stale_evidence return value (affected entity IDs)."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.services.mental_model_cleanup import prune_stale_evidence
from memex_core.memory.sql_models import MentalModel, Observation, EvidenceItem


def _make_model(entity_id, vault_id, observations):
    """Build a MentalModel with the given observations (list of Observation dicts)."""
    model = MentalModel(
        entity_id=entity_id,
        vault_id=vault_id,
        observations=[obs.model_dump(mode='json') for obs in observations],
    )
    return model


def _make_observation(unit_ids):
    """Build an Observation with evidence pointing to the given unit_ids."""
    return Observation(
        id=uuid4(),
        title='test observation title',
        content='test observation',
        evidence=[EvidenceItem(memory_id=uid, snippet='evidence snippet') for uid in unit_ids],
    )


@pytest.mark.asyncio
async def test_returns_empty_set_when_no_entities():
    session = AsyncMock(spec=AsyncSession)
    result = await prune_stale_evidence(session, set(), [uuid4()], uuid4())
    assert result == set()


@pytest.mark.asyncio
async def test_returns_empty_set_when_no_deleted_units():
    session = AsyncMock(spec=AsyncSession)
    result = await prune_stale_evidence(session, {uuid4()}, [], uuid4())
    assert result == set()


@pytest.mark.asyncio
async def test_returns_affected_entity_id_when_evidence_pruned():
    """Entity whose model has evidence pruned should be in the returned set."""
    session = AsyncMock(spec=AsyncSession)
    vault_id = uuid4()
    entity_id = uuid4()
    deleted_unit = uuid4()
    surviving_unit = uuid4()

    obs = _make_observation([deleted_unit, surviving_unit])
    model = _make_model(entity_id, vault_id, [obs])

    mock_result = MagicMock()
    mock_result.all.return_value = [model]
    session.exec = AsyncMock(return_value=mock_result)

    result = await prune_stale_evidence(session, {entity_id}, [deleted_unit], vault_id)

    assert entity_id in result
    # Model should still exist (has surviving evidence)
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_empty_when_no_evidence_pruned():
    """Entity whose model has no evidence matching deleted units should not be affected."""
    session = AsyncMock(spec=AsyncSession)
    vault_id = uuid4()
    entity_id = uuid4()
    surviving_unit = uuid4()

    obs = _make_observation([surviving_unit])
    model = _make_model(entity_id, vault_id, [obs])

    mock_result = MagicMock()
    mock_result.all.return_value = [model]
    session.exec = AsyncMock(return_value=mock_result)

    result = await prune_stale_evidence(session, {entity_id}, [uuid4()], vault_id)

    assert entity_id not in result


@pytest.mark.asyncio
async def test_returns_entity_when_model_deleted():
    """When all observations lose all evidence, the model is deleted and entity is affected."""
    session = AsyncMock(spec=AsyncSession)
    vault_id = uuid4()
    entity_id = uuid4()
    deleted_unit = uuid4()

    obs = _make_observation([deleted_unit])
    model = _make_model(entity_id, vault_id, [obs])

    mock_result = MagicMock()
    mock_result.all.return_value = [model]
    session.exec = AsyncMock(return_value=mock_result)

    result = await prune_stale_evidence(session, {entity_id}, [deleted_unit], vault_id)

    assert entity_id in result
    session.delete.assert_awaited_once_with(model)


@pytest.mark.asyncio
async def test_returns_empty_when_no_models_exist():
    """Entity with no mental models should not be in the affected set."""
    session = AsyncMock(spec=AsyncSession)
    vault_id = uuid4()
    entity_id = uuid4()

    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec = AsyncMock(return_value=mock_result)

    result = await prune_stale_evidence(session, {entity_id}, [uuid4()], vault_id)

    assert result == set()
