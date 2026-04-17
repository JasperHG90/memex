"""Tests for the memex_get_memory_links MCP tool."""

from uuid import uuid4

import pytest
from memex_common.schemas import MemoryLinkDTO

from helpers import parse_tool_result


@pytest.mark.asyncio
async def test_get_memory_links_returns_links(mock_api, mcp_client):
    """Valid UUID list returns links from the API."""
    uid1 = uuid4()
    uid2 = uuid4()
    target_uid = uuid4()
    note_id = uuid4()

    mock_api.get_memory_links.return_value = {
        uid1: [
            MemoryLinkDTO(
                unit_id=target_uid,
                note_id=note_id,
                note_title='Target Note',
                relation='temporal',
                weight=0.8,
            ),
        ],
        uid2: [
            MemoryLinkDTO(
                unit_id=target_uid,
                note_id=note_id,
                note_title='Target Note',
                relation='contradicts',
                weight=0.9,
            ),
        ],
    }

    result = await mcp_client.call_tool(
        'memex_get_memory_links',
        {'unit_ids': [str(uid1), str(uid2)]},
    )
    data = parse_tool_result(result)

    assert len(data) == 2
    relations = {d['relation'] for d in data}
    assert 'temporal' in relations
    assert 'contradicts' in relations


@pytest.mark.asyncio
async def test_get_memory_links_empty_list(mock_api, mcp_client):
    """Empty unit_ids list returns empty result."""
    result = await mcp_client.call_tool(
        'memex_get_memory_links',
        {'unit_ids': []},
    )
    data = parse_tool_result(result)

    assert data == []
    # get_memory_links should not be called for empty input
    mock_api.get_memory_links.assert_not_called()


@pytest.mark.asyncio
async def test_get_memory_links_link_type_filter(mock_api, mcp_client):
    """link_type filter is passed through to the API."""
    uid = uuid4()
    mock_api.get_memory_links.return_value = {
        uid: [
            MemoryLinkDTO(
                unit_id=uuid4(),
                relation='contradicts',
                weight=0.9,
            ),
        ],
    }

    result = await mcp_client.call_tool(
        'memex_get_memory_links',
        {'unit_ids': [str(uid)], 'link_type': 'contradicts'},
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['relation'] == 'contradicts'

    # Verify link_types filter was passed through
    call_kwargs = mock_api.get_memory_links.call_args
    assert call_kwargs[1]['link_types'] == ['contradicts']


@pytest.mark.asyncio
async def test_get_memory_links_invalid_uuids_skipped(mock_api, mcp_client):
    """Invalid UUIDs are silently skipped."""
    valid_uid = uuid4()
    mock_api.get_memory_links.return_value = {}

    result = await mcp_client.call_tool(
        'memex_get_memory_links',
        {'unit_ids': ['not-a-uuid', 'also-invalid', str(valid_uid)]},
    )
    data = parse_tool_result(result)

    assert data == []
    # Only the valid UUID should have been passed
    call_args = mock_api.get_memory_links.call_args
    assert len(call_args[0][0]) == 1
    assert call_args[0][0][0] == valid_uid


@pytest.mark.asyncio
async def test_get_memory_links_all_invalid_uuids(mock_api, mcp_client):
    """When all UUIDs are invalid, returns empty without calling API."""
    result = await mcp_client.call_tool(
        'memex_get_memory_links',
        {'unit_ids': ['bad1', 'bad2']},
    )
    data = parse_tool_result(result)

    assert data == []
    mock_api.get_memory_links.assert_not_called()
