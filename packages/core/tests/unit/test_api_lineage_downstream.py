import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from memex_common.schemas import LineageDirection
from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note


@pytest.mark.asyncio
async def test_get_lineage_downstream_document_to_mental_model(api, mock_session):
    # Setup IDs
    doc_id = uuid4()
    unit_id = uuid4()
    obs_id = uuid4()
    mm_id = uuid4()

    # Data
    doc = Note(id=doc_id, vault_id=uuid4())
    unit = MemoryUnit(id=unit_id, text='Test Unit', fact_type='event', note_id=doc_id)

    obs_data = {
        'id': str(obs_id),
        'title': 'Test Obs',
        'content': 'Test Obs Detail',
        'evidence': [{'memory_id': str(unit_id)}],
    }

    mm = MentalModel(id=mm_id, entity_id=uuid4(), vault_id=uuid4(), observations=[obs_data])

    # Mock session.exec for all queries (session.get is no longer used)
    mock_doc_result = MagicMock()
    mock_doc_result.first.return_value = doc

    mock_units_result = MagicMock()
    mock_units_result.all.return_value = [unit]

    mock_unit_result = MagicMock()
    mock_unit_result.first.return_value = unit

    mock_mm_result = MagicMock()
    mock_mm_result.all.return_value = [mm]
    mock_mm_result.first.return_value = mm

    mock_session.exec.side_effect = [
        mock_doc_result,  # select(Note) for doc
        mock_units_result,  # select(MemoryUnit) for doc -> units
        mock_unit_result,  # select(MemoryUnit) for unit by id
        mock_mm_result,  # select(MentalModel) for unit -> observations
        mock_mm_result,  # select(MentalModel) for observation -> parent mm
        mock_mm_result,  # select(MentalModel) for mental_model leaf
    ]

    result = await api.get_lineage(
        entity_type='note', entity_id=doc_id, direction=LineageDirection.DOWNSTREAM, depth=3
    )

    assert result.entity_type == 'note'
    assert str(result.entity['id']) == str(doc_id)
    assert len(result.derived_from) == 1

    child = result.derived_from[0]
    assert child.entity_type == 'memory_unit'
    assert str(child.entity['id']) == str(unit_id)

    grandchild = child.derived_from[0]
    assert grandchild.entity_type == 'observation'
    assert str(grandchild.entity['id']) == str(obs_id)

    great_grandchild = grandchild.derived_from[0]
    assert great_grandchild.entity_type == 'mental_model'
    assert str(great_grandchild.entity['id']) == str(mm.id)


@pytest.mark.asyncio
async def test_lineage_downstream_excludes_heavy_fields(api, mock_session):
    """Verify that lineage responses don't contain embeddings, context, etc."""
    doc_id = uuid4()
    unit_id = uuid4()

    doc = Note(
        id=doc_id,
        vault_id=uuid4(),
        title='Test Note',
        original_text='A very long document text...',
        page_index={'nodes': [{'id': '1', 'title': 'Section 1'}]},
    )
    unit = MemoryUnit(
        id=unit_id,
        text='Test fact',
        fact_type='world',
        note_id=doc_id,
        embedding=[0.1] * 384,
        context='A very long context string...',
    )

    mock_doc_result = MagicMock()
    mock_doc_result.first.return_value = doc

    mock_units_result = MagicMock()
    mock_units_result.all.return_value = [unit]

    mock_unit_result = MagicMock()
    mock_unit_result.first.return_value = unit

    mock_empty = MagicMock()
    mock_empty.all.return_value = []

    mock_session.exec.side_effect = [
        mock_doc_result,  # select(Note)
        mock_units_result,  # select(MemoryUnit) children
        mock_unit_result,  # select(MemoryUnit) by id
        mock_empty,  # select(MentalModel) for unit -> observations
    ]

    result = await api.get_lineage(
        entity_type='note', entity_id=doc_id, direction=LineageDirection.DOWNSTREAM, depth=2
    )

    # Note should not contain heavy fields
    note_entity = result.entity
    assert 'original_text' not in note_entity
    assert 'page_index' not in note_entity
    assert 'session_id' not in note_entity
    assert 'content_hash' not in note_entity
    assert 'filestore_path' not in note_entity
    # Note should contain expected fields
    assert 'id' in note_entity
    assert 'title' in note_entity

    # MemoryUnit should not contain heavy fields
    unit_entity = result.derived_from[0].entity
    assert 'embedding' not in unit_entity
    assert 'context' not in unit_entity
    assert 'search_tsvector' not in unit_entity
    assert 'chunk_id' not in unit_entity
    assert 'access_count' not in unit_entity
    # MemoryUnit should contain expected fields
    assert 'id' in unit_entity
    assert 'text' in unit_entity
    assert 'fact_type' in unit_entity
