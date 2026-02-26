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
    doc = Note(id=doc_id, content='Test Doc', vault_id=uuid4())
    unit = MemoryUnit(id=unit_id, text='Test Unit', fact_type='event', note_id=doc_id)

    obs_data = {'id': str(obs_id), 'content': 'Test Obs', 'evidence': [{'memory_id': str(unit_id)}]}

    mm = MentalModel(id=mm_id, entity_id=uuid4(), vault_id=uuid4(), observations=[obs_data])

    # Mocking for Downstream

    # 1. Get Document and MemoryUnit
    async def mock_get(model, id):
        if model == Note and str(id) == str(doc_id):
            return doc
        if model == MemoryUnit and str(id) == str(unit_id):
            return unit
        return None

    mock_session.get.side_effect = mock_get

    mock_units_result = MagicMock()
    mock_units_result.all.return_value = [unit]

    mock_mm_result = MagicMock()
    mock_mm_result.all.return_value = [mm]
    mock_mm_result.first.return_value = mm

    mock_session.exec.side_effect = [
        mock_units_result,  # For Document -> MemoryUnits
        mock_mm_result,  # For MemoryUnit -> MentalModels
        mock_mm_result,  # For Observation -> Parent MentalModel
        mock_mm_result,  # For MentalModel -> MentalModel (leaf)
    ]

    result = await api.get_lineage(
        entity_type='document', entity_id=doc_id, direction=LineageDirection.DOWNSTREAM, depth=3
    )

    assert result.entity_type == 'document'
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
