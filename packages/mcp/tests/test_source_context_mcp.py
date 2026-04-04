"""Tests for source_context MCP parameter and memex_search_user_notes tool (AC-B05, AC-B06)."""

import pytest
from uuid import uuid4

from conftest import parse_tool_result
from memex_common.schemas import MemoryUnitDTO, FactTypes


@pytest.mark.asyncio
async def test_memory_search_passes_source_context(mock_api, mcp_client):
    """AC-B05: memex_memory_search accepts source_context and passes it to the API."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search',
        {
            'query': 'my annotations',
            'vault_ids': ['test-vault'],
            'source_context': 'user_notes',
        },
    )

    call_args = mock_api.search.call_args
    assert call_args is not None
    assert call_args.kwargs.get('source_context') == 'user_notes'


@pytest.mark.asyncio
async def test_memory_search_source_context_defaults_none(mock_api, mcp_client):
    """When source_context is omitted, it should default to None."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search',
        {'query': 'test query', 'vault_ids': ['test-vault']},
    )

    call_args = mock_api.search.call_args
    assert call_args is not None
    assert call_args.kwargs.get('source_context') is None


@pytest.mark.asyncio
async def test_search_user_notes_tool_exists():
    """AC-B06: memex_search_user_notes tool exists."""
    from memex_mcp.server import mcp

    tools = await mcp._list_tools()
    tool_names = [t.name for t in tools]
    assert 'memex_search_user_notes' in tool_names


@pytest.mark.asyncio
async def test_search_user_notes_hardcodes_context(mock_api, mcp_client):
    """AC-B06: memex_search_user_notes hardcodes source_context='user_notes'."""
    unit_id = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=unit_id,
            note_id=uuid4(),
            text='My annotation about project architecture.',
            fact_type=FactTypes.WORLD,
            score=0.9,
            vault_id=uuid4(),
            metadata={},
        )
    ]

    result = await mcp_client.call_tool(
        'memex_search_user_notes',
        {'query': 'project architecture', 'vault_ids': ['test-vault']},
    )

    # Verify source_context was hardcoded
    call_args = mock_api.search.call_args
    assert call_args is not None
    assert call_args.kwargs.get('source_context') == 'user_notes'

    # Verify results are returned
    data = parse_tool_result(result)
    assert len(data) == 1
    assert data[0]['id'] == str(unit_id)
