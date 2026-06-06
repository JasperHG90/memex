"""Pin: no MCP tool output ever carries an ``embedding`` key.

Vector exposure is an HTTP/Python-caller capability, deliberately withheld
from agent surfaces. MCP tools render their own projection models
(``McpFact`` / ``McpKVEntry`` / explicit dicts), so even DTOs that arrive
from the client WITH vectors populated must serialize without an
``embedding`` key. These tests feed vector-laden DTOs through the tools and
assert the rendered output stays vector-free, so a future refactor that
starts passing common DTOs through raw fails loudly.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from uuid import uuid4

import pytest

from helpers import parse_tool_result
from memex_common.schemas import FactTypes, MemoryUnitDTO, VaultSummaryDTO


def _walk(obj):
    """Yield every dict reachable in a parsed tool result."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _assert_no_embedding_key(data) -> None:
    for node in _walk(data):
        assert 'embedding' not in node, f'embedding key leaked into MCP output: {node.keys()}'


def _vector_laden_unit(vault_id, chunk_id) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        text='unit with a vector attached',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=uuid4(),
        vault_id=vault_id,
        metadata={},
        mentioned_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        chunk_id=chunk_id,
        embedding=[0.1] * 8,
    )


@pytest.mark.asyncio
async def test_get_memory_units_output_has_no_embedding(mock_api, mock_config, mcp_client):
    vault_uuid = uuid4()
    chunk_id = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_memory_units_by_chunks.return_value = [_vector_laden_unit(vault_uuid, chunk_id)]

    result = await mcp_client.call_tool(
        'memex_get_memory_units',
        {'chunk_ids': [str(chunk_id)], 'vault_id': 'my-vault'},
    )
    data = parse_tool_result(result)
    assert data, 'expected at least one unit in the output'
    _assert_no_embedding_key(data)


@pytest.mark.asyncio
async def test_kv_get_output_has_no_embedding(mock_api, mock_config, mcp_client):
    mock_api.kv_get.return_value = SimpleNamespace(
        id=uuid4(),
        key='user:editor',
        value='neovim',
        embedding=[0.5] * 8,
        expires_at=None,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )

    result = await mcp_client.call_tool('memex_kv_get', {'key': 'user:editor'})
    data = parse_tool_result(result)
    assert data is not None
    _assert_no_embedding_key(data)


@pytest.mark.asyncio
async def test_get_vault_summary_output_has_no_embedding(mock_api, mock_config, mcp_client):
    vault_uuid = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_vault_summary.return_value = VaultSummaryDTO(
        id=uuid4(),
        vault_id=vault_uuid,
        narrative='A vault with a stored narrative vector.',
        themes=[],
        inventory={},
        key_entities=[],
        embedding=[0.25] * 8,
        version=2,
        notes_incorporated=4,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )

    result = await mcp_client.call_tool('memex_get_vault_summary', {'vault_id': str(vault_uuid)})
    data = parse_tool_result(result)
    assert data is not None
    _assert_no_embedding_key(data)
