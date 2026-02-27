import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from memex_common.schemas import LineageDirection
from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note


@pytest.mark.asyncio
async def test_get_lineage_upstream_observation_to_document(api, mock_session):
    # Setup IDs
    obs_id = uuid4()
    unit_id = uuid4()
    doc_id = uuid4()
    mm_id = uuid4()

    # Mock Entities
    # Observation is now a dict inside MentalModel
    obs_data = {'id': str(obs_id), 'content': 'Test Obs', 'evidence': [{'memory_id': str(unit_id)}]}

    mm = MentalModel(id=mm_id, entity_id=uuid4(), vault_id=uuid4(), observations=[obs_data])

    unit = MemoryUnit(id=unit_id, text='Test Unit', fact_type='event', note_id=doc_id)
    doc = Note(id=doc_id, content='Test Doc', vault_id=uuid4())

    # Mock session.exec for MentalModel lookup (for Observation)
    # and session.get for MemoryUnit and Document

    mock_result = MagicMock()
    mock_result.first.return_value = mm
    mock_session.exec.return_value = mock_result

    async def mock_get(model, id):
        if model == MemoryUnit and str(id) == str(unit_id):
            return unit
        if model == Note and str(id) == str(doc_id):
            return doc
        return None

    mock_session.get.side_effect = mock_get

    # Execute
    result = await api.get_lineage(
        entity_type='observation',
        entity_id=obs_id,
        direction=LineageDirection.UPSTREAM,
        depth=3,
    )

    # Verify
    assert result.entity_type == 'observation'
    assert str(result.entity['id']) == str(obs_id)
    assert len(result.derived_from) == 1

    child = result.derived_from[0]
    assert child.entity_type == 'memory_unit'
    assert str(child.entity['id']) == str(unit_id)

    grandchild = child.derived_from[0]
    assert grandchild.entity_type == 'note'
    assert str(grandchild.entity['id']) == str(doc_id)


@pytest.mark.asyncio
async def test_get_lineage_upstream_mental_model(api, mock_session):
    # Test starting from MentalModel
    mm_id = uuid4()
    entity_id = uuid4()
    obs_id = uuid4()

    obs_data = {'id': str(obs_id), 'content': 'Test Obs'}

    mm = MentalModel(id=mm_id, entity_id=entity_id, vault_id=uuid4(), observations=[obs_data])

    # Mock exec for MentalModel lookup
    mock_result = MagicMock()
    mock_result.first.return_value = mm
    mock_session.exec.return_value = mock_result

    # Execute
    result = await api.get_lineage(
        entity_type='mental_model',
        entity_id=entity_id,  # Keyed by entity_id
        direction=LineageDirection.UPSTREAM,
        depth=3,
    )

    assert result.entity_type == 'mental_model'
    assert str(result.entity['entity_id']) == str(entity_id)
    assert len(result.derived_from) == 1
    assert result.derived_from[0].entity_type == 'observation'
    assert str(result.derived_from[0].entity['id']) == str(obs_id)
