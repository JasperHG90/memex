"""MCP tool tests for ``memex_append_note``.

Drives the tool through the FastMCP ``Client`` (via the ``mcp_client``
fixture) with the ``RemoteMemexAPI`` patched. Validates:

* Tool is registered with the expected schema (delta required, append_id +
  joiner optional, note_key/vault_id/note_id paired).
* The tool builds a ``NoteAppendRequest`` and forwards it to the API.
* Success and idempotent-replay payloads round-trip via ``McpAppendNoteResult``.
* Auto-generation of ``append_id`` when omitted.
* Identifier validation surfaces ``ToolError`` to the caller.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from helpers import parse_tool_result

from memex_common.schemas import NoteAppendRequest, NoteAppendResponse
from memex_mcp.server import mcp


def _response(note_id: UUID, append_id: UUID, *, status: str = 'success') -> NoteAppendResponse:
    return NoteAppendResponse(
        status=status,
        note_id=note_id,
        append_id=append_id,
        content_hash='abc',
        delta_bytes=5,
        new_unit_ids=[],
    )


@pytest.mark.asyncio
async def test_append_note_registered_with_expected_schema():
    """The tool advertises its required parameters via FastMCP introspection.

    Discovery mode hides individual tools from ``client.list_tools()``, so we
    introspect the server's full tool set directly via ``mcp._list_tools()``.
    """
    tools = await mcp._list_tools()
    tool = next((t for t in tools if t.name == 'memex_append_note'), None)
    assert tool is not None, 'memex_append_note must be registered'
    assert 'write' in tool.tags

    schema = tool.parameters
    props = schema.get('properties', {})
    # delta is the only strict requirement at the schema level.
    assert 'delta' in schema.get('required', []), schema
    # All forms of identifying the note are present as optional fields.
    for name in ('note_key', 'vault_id', 'note_id', 'append_id', 'joiner', 'user_notes'):
        assert name in props


@pytest.mark.asyncio
async def test_append_note_success_round_trips(mock_api, mcp_client):
    """Happy path: forwards a NoteAppendRequest and returns McpAppendNoteResult."""
    note_id = uuid4()
    append_id = uuid4()
    mock_api.append_to_note.return_value = _response(note_id, append_id)

    result = await mcp_client.call_tool(
        'memex_append_note',
        {
            'note_key': 'session-2026',
            'vault_id': 'global',
            'delta': 'a new line',
            'append_id': str(append_id),
        },
    )

    data = parse_tool_result(result)
    assert data['note_id'] == str(note_id)
    assert data['append_id'] == str(append_id)
    assert data['status'] == 'success'
    assert data['delta_bytes'] == 5
    assert data['new_unit_count'] == 0

    mock_api.append_to_note.assert_called_once()
    sent = mock_api.append_to_note.call_args.args[0]
    assert isinstance(sent, NoteAppendRequest)
    assert sent.note_key == 'session-2026'
    assert sent.vault_id == 'global'
    assert sent.delta == 'a new line'
    assert sent.append_id == append_id


@pytest.mark.asyncio
async def test_append_note_replay_status_is_passed_through(mock_api, mcp_client):
    note_id = uuid4()
    append_id = uuid4()
    mock_api.append_to_note.return_value = _response(note_id, append_id, status='replayed')

    result = await mcp_client.call_tool(
        'memex_append_note',
        {
            'note_id': str(note_id),
            'delta': 'x',
            'append_id': str(append_id),
        },
    )
    data = parse_tool_result(result)
    assert data['status'] == 'replayed'
    assert data['note_id'] == str(note_id)


@pytest.mark.asyncio
async def test_append_note_auto_generates_append_id(mock_api, mcp_client):
    """Calls without append_id receive a fresh uuid4 each time."""
    note_id = uuid4()

    def _fake_append(req: NoteAppendRequest):
        return _response(note_id, req.append_id)

    mock_api.append_to_note.side_effect = _fake_append

    await mcp_client.call_tool('memex_append_note', {'note_id': str(note_id), 'delta': 'one'})
    await mcp_client.call_tool('memex_append_note', {'note_id': str(note_id), 'delta': 'two'})

    a, b = (c.args[0].append_id for c in mock_api.append_to_note.call_args_list)
    assert a != b


@pytest.mark.asyncio
async def test_append_note_missing_identifier_returns_tool_error(mock_api, mcp_client):
    """Without note_key or note_id the tool raises a ToolError."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='note_key|note_id'):
        await mcp_client.call_tool('memex_append_note', {'delta': 'x'})
    mock_api.append_to_note.assert_not_called()


@pytest.mark.asyncio
async def test_append_note_key_without_vault_returns_tool_error(mock_api, mcp_client):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='vault_id'):
        await mcp_client.call_tool('memex_append_note', {'note_key': 'k', 'delta': 'x'})
    mock_api.append_to_note.assert_not_called()
