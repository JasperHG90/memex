"""Tests for newly added MCP tools."""

import datetime as dt

import pytest
from fastmcp.exceptions import ToolError
from uuid import uuid4

from memex_common.schemas import (
    EntityDTO,
    MemoryUnitDTO,
    NoteDTO,
    VaultDTO,
    FactTypes,
)


# ── memex_list_vaults ──


@pytest.mark.asyncio
async def test_list_vaults_returns_formatted_list(mock_api, mcp_client):
    v1 = VaultDTO(id=uuid4(), name='Personal', description='My vault')
    v2 = VaultDTO(id=uuid4(), name='Work', description=None)
    mock_api.list_vaults.return_value = [v1, v2]

    result = await mcp_client.call_tool('memex_list_vaults', {})
    text = result.content[0].text

    assert 'Found 2 vault(s)' in text
    assert '**Personal**' in text
    assert str(v1.id) in text
    assert 'My vault' in text
    assert '**Work**' in text


@pytest.mark.asyncio
async def test_list_vaults_empty(mock_api, mcp_client):
    mock_api.list_vaults.return_value = []

    result = await mcp_client.call_tool('memex_list_vaults', {})
    assert 'No vaults found' in result.content[0].text


@pytest.mark.asyncio
async def test_list_vaults_error_raises_tool_error(mock_api, mcp_client):
    mock_api.list_vaults.side_effect = RuntimeError('connection refused')

    with pytest.raises(ToolError, match='connection refused'):
        await mcp_client.call_tool('memex_list_vaults', {})


# ── memex_list_notes ──


@pytest.mark.asyncio
async def test_list_notes_returns_formatted_list(mock_api, mcp_client):
    n1 = NoteDTO(
        id=uuid4(),
        title='First Note',
        vault_id=uuid4(),
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
    )
    mock_api.list_notes.return_value = [n1]

    result = await mcp_client.call_tool('memex_list_notes', {'limit': 10, 'offset': 0})
    text = result.content[0].text

    assert 'Found 1 note(s)' in text
    assert '**First Note**' in text
    assert str(n1.id) in text
    mock_api.list_notes.assert_called_once_with(limit=10, offset=0, vault_id=None)


@pytest.mark.asyncio
async def test_list_notes_empty(mock_api, mcp_client):
    mock_api.list_notes.return_value = []

    result = await mcp_client.call_tool('memex_list_notes', {})
    assert 'No notes found' in result.content[0].text


@pytest.mark.asyncio
async def test_list_notes_error_raises_tool_error(mock_api, mcp_client):
    mock_api.list_notes.side_effect = RuntimeError('timeout')

    with pytest.raises(ToolError, match='timeout'):
        await mcp_client.call_tool('memex_list_notes', {})


# ── memex_list_entities ──


@pytest.mark.asyncio
async def test_list_entities_ranked(mock_api, mcp_client):
    """Without a query, should call list_entities_ranked."""
    e1 = EntityDTO(id=uuid4(), name='Python', mention_count=42)

    async def _ranked(limit: int = 100, vault_id=None):
        yield e1

    mock_api.list_entities_ranked = _ranked

    result = await mcp_client.call_tool('memex_list_entities', {})
    text = result.content[0].text

    assert '**Python**' in text
    assert 'mentions: 42' in text


@pytest.mark.asyncio
async def test_list_entities_with_query(mock_api, mcp_client):
    """With a query, should call search_entities."""
    e1 = EntityDTO(id=uuid4(), name='Rust', mention_count=10)
    mock_api.search_entities.return_value = [e1]

    result = await mcp_client.call_tool('memex_list_entities', {'query': 'rust'})
    text = result.content[0].text

    assert '**Rust**' in text
    mock_api.search_entities.assert_called_once_with('rust', limit=20, vault_id=None)


@pytest.mark.asyncio
async def test_list_entities_empty(mock_api, mcp_client):
    mock_api.search_entities.return_value = []

    result = await mcp_client.call_tool('memex_list_entities', {'query': 'nonexistent'})
    assert 'No entities found' in result.content[0].text


# ── memex_get_entity ──


@pytest.mark.asyncio
async def test_get_entity_success(mock_api, mcp_client):
    eid = uuid4()
    vid = uuid4()
    mock_api.get_entity.return_value = EntityDTO(
        id=eid, name='Docker', mention_count=15, vault_id=vid
    )

    result = await mcp_client.call_tool('memex_get_entity', {'entity_id': str(eid)})
    text = result.content[0].text

    assert '# Entity: Docker' in text
    assert str(eid) in text
    assert '**Mentions:** 15' in text
    assert str(vid) in text


@pytest.mark.asyncio
async def test_get_entity_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Entity UUID'):
        await mcp_client.call_tool('memex_get_entity', {'entity_id': 'not-valid'})


@pytest.mark.asyncio
async def test_get_entity_api_error(mock_api, mcp_client):
    eid = uuid4()
    mock_api.get_entity.side_effect = RuntimeError('not found')

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool('memex_get_entity', {'entity_id': str(eid)})


# ── memex_get_entity_mentions ──


@pytest.mark.asyncio
async def test_get_entity_mentions_success(mock_api, mcp_client):
    eid = uuid4()
    uid = uuid4()
    nid = uuid4()
    mock_api.get_entity_mentions.return_value = [
        {
            'unit': {'id': str(uid), 'text': 'Python is great', 'fact_type': 'world'},
            'note': {'id': str(nid)},
        }
    ]

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    text = result.content[0].text

    assert 'Found 1 mention(s)' in text
    assert 'Python is great' in text
    assert str(uid) in text
    assert str(nid) in text


@pytest.mark.asyncio
async def test_get_entity_mentions_empty(mock_api, mcp_client):
    eid = uuid4()
    mock_api.get_entity_mentions.return_value = []

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    assert 'No mentions found' in result.content[0].text


@pytest.mark.asyncio
async def test_get_entity_mentions_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Entity UUID'):
        await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': 'bad'})


# ── memex_get_entity_cooccurrences ──


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_success(mock_api, mcp_client):
    eid = uuid4()
    other_id = uuid4()
    mock_api.get_entity_cooccurrences.return_value = [
        {'entity_id_1': str(eid), 'entity_id_2': str(other_id), 'cooccurrence_count': 7}
    ]

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    text = result.content[0].text

    assert 'Found 1 co-occurring' in text
    assert str(other_id) in text
    assert 'co-occurrences: 7' in text


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_empty(mock_api, mcp_client):
    eid = uuid4()
    mock_api.get_entity_cooccurrences.return_value = []

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    assert 'No co-occurrences found' in result.content[0].text


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Entity UUID'):
        await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': 'nope'})


# ── memex_get_memory_unit ──


@pytest.mark.asyncio
async def test_get_memory_unit_success(mock_api, mcp_client):
    uid = uuid4()
    nid = uuid4()
    ts = dt.datetime(2025, 3, 1, 10, 0, tzinfo=dt.timezone.utc)
    mock_api.get_memory_unit.return_value = MemoryUnitDTO(
        id=uid,
        text='Docker containers isolate processes.',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=nid,
        vault_id=uuid4(),
        metadata={'source': 'docs'},
        mentioned_at=ts,
    )

    result = await mcp_client.call_tool('memex_get_memory_unit', {'unit_id': str(uid)})
    text = result.content[0].text

    assert '# Memory Unit' in text
    assert str(uid) in text
    assert '**Type:** world' in text
    assert str(nid) in text
    assert 'Docker containers isolate processes.' in text
    assert '2025-03-01' in text
    assert "{'source': 'docs'}" in text


@pytest.mark.asyncio
async def test_get_memory_unit_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Unit UUID'):
        await mcp_client.call_tool('memex_get_memory_unit', {'unit_id': 'invalid'})


@pytest.mark.asyncio
async def test_get_memory_unit_api_error(mock_api, mcp_client):
    uid = uuid4()
    mock_api.get_memory_unit.side_effect = RuntimeError('unit not found')

    with pytest.raises(ToolError, match='unit not found'):
        await mcp_client.call_tool('memex_get_memory_unit', {'unit_id': str(uid)})


# ── memex_ingest_url ──


@pytest.mark.asyncio
async def test_ingest_url_background(mock_api, mcp_client):
    mock_api.ingest_url.return_value = {'status': 'queued', 'job_id': str(uuid4())}

    result = await mcp_client.call_tool('memex_ingest_url', {'url': 'https://example.com/article'})
    text = result.content[0].text

    assert 'URL ingestion queued' in text
    assert 'Status: queued' in text
    mock_api.ingest_url.assert_called_once()


@pytest.mark.asyncio
async def test_ingest_url_foreground(mock_api, mcp_client):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.note_id = str(uuid4())
    mock_api.ingest_url.return_value = resp

    result = await mcp_client.call_tool(
        'memex_ingest_url', {'url': 'https://example.com', 'background': False}
    )
    text = result.content[0].text

    assert 'URL ingested' in text


@pytest.mark.asyncio
async def test_ingest_url_error(mock_api, mcp_client):
    mock_api.ingest_url.side_effect = RuntimeError('network error')

    with pytest.raises(ToolError, match='network error'):
        await mcp_client.call_tool('memex_ingest_url', {'url': 'https://bad.com'})
