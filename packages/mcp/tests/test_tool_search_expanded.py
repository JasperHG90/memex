import pytest
from uuid import uuid4
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

    result = await mcp_client.call_tool('memex_memory_search', {'query': 'python', 'limit': 1})

    output_text = result.content[0].text
    assert 'Found 1 results' in output_text
    assert f'[Unit: {unit_id}]' in output_text
    assert f'[Note: {doc_id}]' in output_text
    assert '[world]' in output_text
    assert '(0.95)' in output_text


@pytest.mark.asyncio
async def test_mcp_search_with_budget(mock_api, mcp_client):
    """Test that search tool propagates token_budget."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search', {'query': 'python', 'limit': 1, 'token_budget': 500}
    )

    # Verify call args
    call_args = mock_api.search.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs.get('token_budget') == 500
