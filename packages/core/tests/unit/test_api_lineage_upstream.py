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
    obs_data = {
        'id': str(obs_id),
        'title': 'Test Obs',
        'content': 'Test Obs Detail',
        'evidence': [{'memory_id': str(unit_id)}],
    }

    mm = MentalModel(id=mm_id, entity_id=uuid4(), vault_id=uuid4(), observations=[obs_data])

    unit = MemoryUnit(id=unit_id, text='Test Unit', fact_type='event', note_id=doc_id)
    doc = Note(id=doc_id, vault_id=uuid4())

    mock_mm_result = MagicMock()
    mock_mm_result.first.return_value = mm

    mock_unit_result = MagicMock()
    mock_unit_result.first.return_value = unit

    mock_doc_result = MagicMock()
    mock_doc_result.first.return_value = doc

    mock_session.exec.side_effect = [
        mock_mm_result,  # select(MentalModel) for observation lookup
        mock_unit_result,  # select(MemoryUnit) for evidence -> unit
        mock_doc_result,  # select(Note) for unit -> doc
    ]

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

    obs_data = {'id': str(obs_id), 'title': 'Test Obs', 'content': 'Test Obs Detail'}

    mm = MentalModel(id=mm_id, entity_id=entity_id, vault_id=uuid4(), observations=[obs_data])

    # Mock exec for MentalModel lookup
    mock_result = MagicMock()
    mock_result.first.return_value = mm
    mock_result.all.return_value = [mm]
    mock_session.exec.return_value = mock_result

    # Execute
    result = await api.get_lineage(
        entity_type='mental_model',
        entity_id=entity_id,
        direction=LineageDirection.UPSTREAM,
        depth=3,
    )

    assert result.entity_type == 'mental_model'
    assert str(result.entity['entity_id']) == str(entity_id)
    assert len(result.derived_from) == 1
    assert result.derived_from[0].entity_type == 'observation'
    assert str(result.derived_from[0].entity['id']) == str(obs_id)


@pytest.mark.asyncio
async def test_lineage_upstream_excludes_heavy_fields(api, mock_session):
    """Verify that upstream lineage excludes heavy fields from all entity types."""
    obs_id = uuid4()
    unit_id = uuid4()
    doc_id = uuid4()
    mm_id = uuid4()

    obs_data = {
        'id': str(obs_id),
        'title': 'Test Obs',
        'content': 'Detailed observation content',
        'trend': 'new',
        'evidence': [{'memory_id': str(unit_id), 'quote': 'some quote'}],
    }

    mm = MentalModel(
        id=mm_id,
        entity_id=uuid4(),
        vault_id=uuid4(),
        observations=[obs_data],
        embedding=[0.1] * 384,
    )

    unit = MemoryUnit(
        id=unit_id,
        text='Test fact',
        fact_type='world',
        note_id=doc_id,
        embedding=[0.1] * 384,
        context='Long context...',
    )
    doc = Note(
        id=doc_id,
        vault_id=uuid4(),
        title='Source Doc',
        original_text='Full original text...',
        page_index={'nodes': []},
    )

    mock_mm_result = MagicMock()
    mock_mm_result.first.return_value = mm

    mock_unit_result = MagicMock()
    mock_unit_result.first.return_value = unit

    mock_doc_result = MagicMock()
    mock_doc_result.first.return_value = doc

    mock_session.exec.side_effect = [
        mock_mm_result,  # select(MentalModel) for observation
        mock_unit_result,  # select(MemoryUnit) for evidence
        mock_doc_result,  # select(Note) for unit -> doc
    ]

    result = await api.get_lineage(
        entity_type='observation',
        entity_id=obs_id,
        direction=LineageDirection.UPSTREAM,
        depth=3,
    )

    # Observation should only have id, title, trend
    obs_entity = result.entity
    assert 'content' not in obs_entity
    assert 'evidence' not in obs_entity
    assert obs_entity['title'] == 'Test Obs'
    assert obs_entity['trend'] == 'new'

    # MemoryUnit
    unit_entity = result.derived_from[0].entity
    assert 'embedding' not in unit_entity
    assert 'context' not in unit_entity

    # Note
    note_entity = result.derived_from[0].derived_from[0].entity
    assert 'original_text' not in note_entity
    assert 'page_index' not in note_entity
