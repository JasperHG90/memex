import pytest
from uuid import uuid4
from conftest import parse_tool_result
from memex_common.schemas import (
    MemoryUnitDTO,
    FactTypes,
)


@pytest.mark.asyncio
async def test_mcp_search_expanded_output(mock_api, mcp_client):
    """Test that search tool output contains Unit ID, Note ID, and Type."""
    unit_id = uuid4()
    doc_id = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=unit_id,
            note_id=doc_id,
            text='Python is a popular programming language.',
            fact_type=FactTypes.WORLD,
            score=0.95,
            vault_id=uuid4(),
            metadata={},
        )
    ]

    result = await mcp_client.call_tool(
        'memex_memory_search', {'query': 'python', 'limit': 1, 'vault_ids': ['test-vault']}
    )

    data = parse_tool_result(result)
    assert len(data) == 1
    unit = data[0]
    assert unit['id'] == str(unit_id)
    assert unit['note_id'] == str(doc_id)
    assert unit['fact_type'] == 'world'
    assert unit['score'] == pytest.approx(0.95, abs=0.01)


@pytest.mark.asyncio
async def test_mcp_search_with_budget(mock_api, mcp_client):
    """Test that search tool propagates token_budget."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search',
        {'query': 'python', 'limit': 1, 'token_budget': 500, 'vault_ids': ['test-vault']},
    )

    # Verify call args
    call_args = mock_api.search.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs.get('token_budget') == 500
