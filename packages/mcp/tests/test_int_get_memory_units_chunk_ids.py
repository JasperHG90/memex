"""F46: ``memex_get_memory_units`` chunk_ids path + XOR validation.

The mock-API substrate is sufficient to exercise the MCP-layer XOR guard
and dispatch logic. End-to-end DB coverage of
``api.get_memory_units_by_chunks`` (vault-scoping, chunk→units traversal)
lives in
``packages/core/tests/integration/test_int_get_memory_units_chunk_ids.py``.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from fastmcp.exceptions import ToolError

from helpers import parse_tool_result
from memex_common.schemas import FactTypes, MemoryUnitDTO


def _build_unit(text: str, chunk_id: UUID, vault_id: UUID) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        text=text,
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=uuid4(),
        vault_id=vault_id,
        metadata={},
        mentioned_at=dt.datetime(2025, 4, 1, tzinfo=dt.timezone.utc),
        chunk_id=chunk_id,
    )


@pytest.mark.asyncio
async def test_chunk_ids_path_returns_units(mock_api, mock_config, mcp_client):
    """``chunk_ids`` invokes ``api.get_memory_units_by_chunks`` and returns its results."""
    vault_uuid = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_uuid

    chunk_a = uuid4()
    chunk_b = uuid4()
    unit_a = _build_unit('from chunk A', chunk_a, vault_uuid)
    unit_b = _build_unit('from chunk B', chunk_b, vault_uuid)
    mock_api.get_memory_units_by_chunks.return_value = [unit_a, unit_b]

    result = await mcp_client.call_tool(
        'memex_get_memory_units',
        {'chunk_ids': [str(chunk_a), str(chunk_b)], 'vault_id': 'my-vault'},
    )
    data = parse_tool_result(result)
    assert len(data) == 2
    texts = {u['text'] for u in data}
    assert {'from chunk A', 'from chunk B'} == texts

    mock_api.get_memory_units_by_chunks.assert_awaited_once()
    call_args = mock_api.get_memory_units_by_chunks.call_args.args
    assert call_args[0] == [chunk_a, chunk_b]
    assert call_args[1] == vault_uuid


@pytest.mark.asyncio
async def test_chunk_ids_unknown_returns_empty(mock_api, mock_config, mcp_client):
    """An unknown chunk_id yields an empty list, not an error."""
    vault_uuid = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_memory_units_by_chunks.return_value = []

    result = await mcp_client.call_tool(
        'memex_get_memory_units',
        {'chunk_ids': [str(uuid4())], 'vault_id': 'my-vault'},
    )
    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_unit_ids_path_unchanged(mock_api, mock_config, mcp_client):
    """The legacy ``unit_ids`` path still routes to per-id ``api.get_memory_unit``."""
    uid = uuid4()
    unit = _build_unit('legacy', uuid4(), uuid4())
    unit.id = uid
    mock_api.get_memory_unit.return_value = unit

    result = await mcp_client.call_tool('memex_get_memory_units', {'unit_ids': [str(uid)]})
    data = parse_tool_result(result)
    assert len(data) == 1
    assert data[0]['id'] == str(uid)
    mock_api.get_memory_units_by_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_xor_both_provided_raises(mock_api, mock_config, mcp_client):
    """Providing both ``unit_ids`` and ``chunk_ids`` is rejected."""
    with pytest.raises(ToolError, match='exactly one'):
        await mcp_client.call_tool(
            'memex_get_memory_units',
            {'unit_ids': [str(uuid4())], 'chunk_ids': [str(uuid4())]},
        )


@pytest.mark.asyncio
async def test_xor_neither_provided_raises(mock_api, mock_config, mcp_client):
    """Providing neither ``unit_ids`` nor ``chunk_ids`` is rejected."""
    with pytest.raises(ToolError, match='exactly one'):
        await mcp_client.call_tool('memex_get_memory_units', {})


@pytest.mark.asyncio
async def test_chunk_ids_invalid_uuids_silently_dropped(mock_api, mock_config, mcp_client):
    """Invalid UUIDs in chunk_ids are skipped; only valid UUIDs reach the API."""
    vault_uuid = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_memory_units_by_chunks.return_value = []
    valid = uuid4()

    result = await mcp_client.call_tool(
        'memex_get_memory_units',
        {'chunk_ids': ['not-a-uuid', str(valid)], 'vault_id': 'my-vault'},
    )
    data = parse_tool_result(result)
    assert data == []
    mock_api.get_memory_units_by_chunks.assert_awaited_once()
    call_args = mock_api.get_memory_units_by_chunks.call_args.args
    assert call_args[0] == [valid]


@pytest.mark.asyncio
async def test_chunk_ids_all_invalid_returns_empty_without_api_call(
    mock_api, mock_config, mcp_client
):
    """If every chunk_id is malformed, no API call is made and result is empty."""
    result = await mcp_client.call_tool(
        'memex_get_memory_units',
        {'chunk_ids': ['nope', 'still-not-a-uuid'], 'vault_id': 'my-vault'},
    )
    data = parse_tool_result(result)
    assert data == []
    mock_api.get_memory_units_by_chunks.assert_not_awaited()
