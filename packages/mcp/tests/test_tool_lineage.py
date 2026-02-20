import pytest
from uuid import uuid4
from memex_common.schemas import LineageResponse, FactTypes


@pytest.mark.asyncio
async def test_mcp_get_lineage(mock_api, mcp_client):
    """Test that lineage tool returns a formatted lineage graph."""
    root_id = uuid4()
    parent_id = uuid4()

    # Mock Lineage Response
    mock_lineage = LineageResponse(
        entity_type='observation',
        entity={'id': str(root_id), 'content': 'Root Observation'},
        derived_from=[
            LineageResponse(
                entity_type='fact',
                entity={'id': str(parent_id), 'text': 'Source Fact', 'fact_type': FactTypes.WORLD},
                derived_from=[],
            )
        ],
    )

    mock_api.get_lineage.return_value = mock_lineage

    # Pass unit_id as string
    result = await mcp_client.call_tool(
        'memex_get_lineage', {'unit_id': str(root_id), 'entity_type': 'observation'}
    )

    output_text = result.content[0].text

    # Verify the tool called the API
    mock_api.get_lineage.assert_called_once_with(entity_id=root_id, entity_type='observation')

    # Verify output format contains key info
    assert 'Root Observation' in output_text
    assert str(root_id) in output_text
    assert 'Source Fact' in output_text
    assert str(parent_id) in output_text
