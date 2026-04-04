"""Tests for memex_update_user_notes MCP tool (AC-C06)."""

import pytest
from uuid import uuid4

from conftest import parse_tool_result


@pytest.mark.asyncio
async def test_update_user_notes_tool_exists():
    """AC-C06: memex_update_user_notes tool exists."""
    from memex_mcp.server import mcp

    tools = await mcp._list_tools()
    tool_names = [t.name for t in tools]
    assert 'memex_update_user_notes' in tool_names


@pytest.mark.asyncio
async def test_update_user_notes_calls_api(mock_api, mcp_client):
    """AC-C06: memex_update_user_notes calls api.update_user_notes."""
    note_id = uuid4()
    mock_api.update_user_notes.return_value = {
        'note_id': str(note_id),
        'units_deleted': 1,
        'units_created': 2,
    }

    result = await mcp_client.call_tool(
        'memex_update_user_notes',
        {'note_id': str(note_id), 'user_notes': 'My annotation'},
    )

    data = parse_tool_result(result)
    assert data['note_id'] == str(note_id)
    assert data['units_deleted'] == 1
    assert data['units_created'] == 2

    mock_api.update_user_notes.assert_called_once()
    call_args = mock_api.update_user_notes.call_args
    assert call_args[0][0] == note_id
    assert call_args[0][1] == 'My annotation'


@pytest.mark.asyncio
async def test_update_user_notes_null(mock_api, mcp_client):
    """AC-C06: passing null user_notes deletes annotations."""
    note_id = uuid4()
    mock_api.update_user_notes.return_value = {
        'note_id': str(note_id),
        'units_deleted': 3,
        'units_created': 0,
    }

    result = await mcp_client.call_tool(
        'memex_update_user_notes',
        {'note_id': str(note_id)},
    )

    data = parse_tool_result(result)
    assert data['units_created'] == 0
