import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from memex_common.schemas import LineageDirection
from memex_core.memory.sql_models import Entity


@pytest.mark.asyncio
async def test_get_lineage_mental_model_stub_from_entity(api, mock_session):
    # Setup
    entity_id = uuid4()

    # Mock session.exec behavior
    # First call: lookup MentalModel -> returns None (simulating missing MM)
    # Second call: lookup Entity -> returns Entity (simulating existing Entity)

    mock_mm_result = MagicMock()
    mock_mm_result.first.return_value = None
    mock_mm_result.all.return_value = []

    mock_ent_result = MagicMock()
    # Create an Entity without vault_id (as per schema)
    # Note: Entity does not have vault_id field.
    ent = Entity(id=entity_id, canonical_name='Test Entity')
    mock_ent_result.first.return_value = ent

    # side_effect for session.exec needs to handle sequence of calls
    # 1. select(MentalModel)
    # 2. select(Entity)
    mock_session.exec.side_effect = [mock_mm_result, mock_ent_result]

    # Execute
    result = await api.get_lineage(
        entity_type='mental_model',
        entity_id=entity_id,
        direction=LineageDirection.UPSTREAM,
        depth=1,
    )

    # Verify
    assert result.entity_type == 'mental_model'
    assert str(result.entity['entity_id']) == str(entity_id)
    # The name should be populated from entity
    assert result.entity['name'] == 'Test Entity'
    # And it should not crash accessing vault_id
