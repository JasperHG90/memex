"""Tests for MCP tools."""

import datetime as dt

import pytest
from fastmcp.exceptions import ToolError
from uuid import uuid4

from uuid import UUID

from helpers import parse_tool_result
from memex_common.schemas import (
    EntityDTO,
    MemoryUnitDTO,
    NoteDTO,
    VaultDTO,
    FactTypes,
)


# ── memex_list_vaults ──


@pytest.mark.asyncio
async def test_list_vaults_returns_formatted_list(mock_api, mock_config, mcp_client):
    v1 = VaultDTO(id=uuid4(), name='Personal', description='My vault')
    v2 = VaultDTO(id=uuid4(), name='Work', description=None)
    now = dt.datetime.now(dt.timezone.utc)
    mock_api.list_vaults_with_counts.return_value = [
        {'vault': v1, 'note_count': 5, 'last_note_added_at': now},
        {'vault': v2, 'note_count': 0, 'last_note_added_at': None},
    ]

    result = await mcp_client.call_tool('memex_list_vaults', {})
    data = parse_tool_result(result)

    assert len(data) == 2
    names = {v['name'] for v in data}
    assert 'Personal' in names
    assert 'Work' in names
    assert any(v['id'] == str(v1.id) for v in data)
    assert any(v['description'] == 'My vault' for v in data)
    assert any(v['note_count'] == 5 for v in data)
    assert any(v.get('last_note_added_at') is not None for v in data)


@pytest.mark.asyncio
async def test_list_vaults_empty(mock_api, mock_config, mcp_client):
    mock_api.list_vaults_with_counts.return_value = []

    result = await mcp_client.call_tool('memex_list_vaults', {})
    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_list_vaults_error_raises_tool_error(mock_api, mcp_client):
    mock_api.list_vaults_with_counts.side_effect = RuntimeError('connection refused')

    with pytest.raises(ToolError, match='connection refused'):
        await mcp_client.call_tool('memex_list_vaults', {})


# ── memex_recent_notes ──


@pytest.mark.asyncio
async def test_recent_notes_returns_formatted_list(mock_api, mcp_client):
    n1 = NoteDTO(
        id=uuid4(),
        title='First Note',
        vault_id=uuid4(),
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
    )
    mock_api.get_recent_notes.return_value = [n1]

    result = await mcp_client.call_tool('memex_recent_notes', {'limit': 10})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'First Note'
    assert data[0]['id'] == str(n1.id)


@pytest.mark.asyncio
async def test_recent_notes_empty(mock_api, mcp_client):
    mock_api.get_recent_notes.return_value = []

    result = await mcp_client.call_tool('memex_recent_notes', {})
    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_recent_notes_error_raises_tool_error(mock_api, mcp_client):
    mock_api.get_recent_notes.side_effect = RuntimeError('timeout')

    with pytest.raises(ToolError, match='timeout'):
        await mcp_client.call_tool('memex_recent_notes', {})


# ── memex_list_entities ──


@pytest.mark.asyncio
async def test_list_entities_ranked(mock_api, mcp_client):
    """Without a query, should call list_entities_ranked."""
    e1 = EntityDTO(id=uuid4(), name='Python', mention_count=42)

    async def _ranked(limit: int = 100, vault_ids=None, entity_type=None):
        yield e1

    mock_api.list_entities_ranked = _ranked

    result = await mcp_client.call_tool('memex_list_entities', {'vault_id': 'test-vault'})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['name'] == 'Python'
    assert data[0]['mention_count'] == 42


@pytest.mark.asyncio
async def test_list_entities_with_query(mock_api, mcp_client):
    """With a query, should call search_entities."""
    e1 = EntityDTO(id=uuid4(), name='Rust', mention_count=10)
    mock_api.search_entities.return_value = [e1]

    result = await mcp_client.call_tool(
        'memex_list_entities', {'query': 'rust', 'vault_id': 'test-vault'}
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['name'] == 'Rust'
    mock_api.search_entities.assert_called_once()
    call_kwargs = mock_api.search_entities.call_args
    assert call_kwargs[0][0] == 'rust'
    assert call_kwargs[1]['limit'] == 20
    assert call_kwargs[1]['entity_type'] is None


@pytest.mark.asyncio
async def test_list_entities_empty(mock_api, mcp_client):
    mock_api.search_entities.return_value = []

    result = await mcp_client.call_tool(
        'memex_list_entities', {'query': 'nonexistent', 'vault_id': 'test-vault'}
    )
    data = parse_tool_result(result)
    assert data == []


# ── memex_get_entities (batch) ──


@pytest.mark.asyncio
async def test_get_entities_batch(mock_api, mcp_client):
    eid1 = uuid4()
    eid2 = uuid4()
    e1 = EntityDTO(id=eid1, name='Docker', mention_count=15, entity_type='Technology')
    e2 = EntityDTO(id=eid2, name='Kubernetes', mention_count=8, entity_type='Technology')
    mock_api.get_entities.return_value = [e1, e2]

    result = await mcp_client.call_tool(
        'memex_get_entities', {'entity_ids': [str(eid1), str(eid2)]}
    )
    data = parse_tool_result(result)

    assert len(data) == 2
    names = {e['name'] for e in data}
    assert 'Docker' in names
    assert 'Kubernetes' in names
    ids = {e['id'] for e in data}
    assert str(eid1) in ids
    assert str(eid2) in ids
    mentions = {e['mention_count'] for e in data}
    assert 15 in mentions
    assert 8 in mentions


@pytest.mark.asyncio
async def test_get_entities_batch_single(mock_api, mcp_client):
    """Single entity should work the same as batch."""
    eid = uuid4()
    vid = uuid4()
    mock_api.get_entities.return_value = [
        EntityDTO(id=eid, name='Docker', mention_count=15, vault_id=vid)
    ]

    result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': [str(eid)]})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['name'] == 'Docker'
    assert data[0]['id'] == str(eid)
    assert data[0]['mention_count'] == 15


@pytest.mark.asyncio
async def test_get_entities_batch_partial_failure(mock_api, mcp_client):
    """Batch should return only found entities; missing IDs are silently dropped."""
    eid1 = uuid4()
    eid2 = uuid4()
    e1 = EntityDTO(id=eid1, name='Docker', mention_count=15)
    # Return only eid1 (eid2 not found)
    mock_api.get_entities.return_value = [e1]

    result = await mcp_client.call_tool(
        'memex_get_entities', {'entity_ids': [str(eid1), str(eid2)]}
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['name'] == 'Docker'
    # eid2 is silently dropped
    ids = {e['id'] for e in data}
    assert str(eid2) not in ids


@pytest.mark.asyncio
async def test_get_entities_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid UUID'):
        await mcp_client.call_tool('memex_get_entities', {'entity_ids': ['not-valid']})


# ── memex_get_entity_mentions ──


@pytest.mark.asyncio
async def test_get_entity_mentions_success(mock_api, mcp_client):
    eid = uuid4()
    uid = uuid4()
    nid = uuid4()
    unit = type('Unit', (), {'id': str(uid), 'text': 'Python is great', 'fact_type': 'world'})()
    doc = type('Doc', (), {'id': str(nid), 'title': 'My Python Note', 'name': None})()
    mock_api.get_entity_mentions.return_value = [{'unit': unit, 'document': doc}]

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['text'] == 'Python is great'
    assert data[0]['unit_id'] == str(uid)
    assert data[0]['note_id'] == str(nid)


@pytest.mark.asyncio
async def test_get_entity_mentions_shows_note_title(mock_api, mcp_client):
    """Mentions should include note title when available."""
    eid = uuid4()
    uid = uuid4()
    nid = uuid4()
    unit = type('Unit', (), {'id': str(uid), 'text': 'Some fact', 'fact_type': 'world'})()
    doc = type('Doc', (), {'id': str(nid), 'title': 'Architecture Overview', 'name': None})()
    mock_api.get_entity_mentions.return_value = [{'unit': unit, 'document': doc}]

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert data[0]['note_title'] == 'Architecture Overview'
    assert data[0]['note_id'] == str(nid)


@pytest.mark.asyncio
async def test_get_entity_mentions_with_note_key(mock_api, mcp_client):
    """When API returns 'note' key (remote client), note_id should still resolve."""
    eid = uuid4()
    uid = uuid4()
    nid = uuid4()
    unit = type(
        'Unit',
        (),
        {'id': str(uid), 'text': 'Data flows here', 'fact_type': 'world', 'note_id': str(nid)},
    )()
    doc = type('Doc', (), {'id': str(nid), 'title': None, 'name': None})()
    mock_api.get_entity_mentions.return_value = [{'unit': unit, 'note': doc}]

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert data[0]['note_id'] == str(nid)


@pytest.mark.asyncio
async def test_get_entity_mentions_no_document_falls_back_to_unit_note_id(mock_api, mcp_client):
    """When neither 'note' nor 'document' key exists, fall back to unit.note_id."""
    eid = uuid4()
    uid = uuid4()
    nid = uuid4()
    unit = type(
        'Unit',
        (),
        {'id': str(uid), 'text': 'Orphan mention', 'fact_type': 'event', 'note_id': str(nid)},
    )()
    mock_api.get_entity_mentions.return_value = [{'unit': unit}]

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert data[0]['note_id'] == str(nid)


@pytest.mark.asyncio
async def test_get_entity_mentions_empty(mock_api, mcp_client):
    eid = uuid4()
    mock_api.get_entity_mentions.return_value = []

    result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_get_entity_mentions_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Entity UUID'):
        await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': 'bad'})


# ── memex_get_entity_cooccurrences ──


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_success(mock_api, mcp_client):
    eid = uuid4()
    other_id = uuid4()
    cooc = {
        'entity_id_1': str(eid),
        'entity_id_2': str(other_id),
        'entity_1_name': 'Memex',
        'entity_1_type': 'Technology',
        'entity_2_name': 'Domain Layer',
        'entity_2_type': 'Technology',
        'cooccurrence_count': 7,
    }
    mock_api.get_entity_cooccurrences.return_value = [cooc]

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['entity_name'] == 'Domain Layer'
    assert data[0]['entity_type'] == 'Technology'
    assert data[0]['entity_id'] == str(other_id)
    assert data[0]['count'] == 7


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_with_limit(mock_api, mcp_client):
    """Limit parameter should be passed through to the API."""
    eid = uuid4()
    mock_api.get_entity_cooccurrences.return_value = []

    await mcp_client.call_tool(
        'memex_get_entity_cooccurrences', {'entity_id': str(eid), 'limit': 5}
    )
    mock_api.get_entity_cooccurrences.assert_called_once_with(UUID(str(eid)), limit=5)


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_empty(mock_api, mcp_client):
    eid = uuid4()
    mock_api.get_entity_cooccurrences.return_value = []

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid Entity UUID'):
        await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': 'nope'})


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_reverse_direction(mock_api, mcp_client):
    """When queried entity is entity_id_2, entity_1 fields should be displayed."""
    eid = uuid4()
    other_id = uuid4()
    cooc = {
        'entity_id_1': str(other_id),
        'entity_id_2': str(eid),
        'entity_1_name': 'PostgreSQL',
        'entity_1_type': 'Technology',
        'entity_2_name': 'Memex',
        'entity_2_type': 'Software',
        'cooccurrence_count': 3,
    }
    mock_api.get_entity_cooccurrences.return_value = [cooc]

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    # Should show entity_1 info (PostgreSQL), not entity_2 (Memex)
    assert data[0]['entity_name'] == 'PostgreSQL'
    assert data[0]['entity_type'] == 'Technology'
    assert data[0]['entity_id'] == str(other_id)
    assert data[0]['count'] == 3


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_no_type(mock_api, mcp_client):
    """When entity_type is None or empty, the type field should reflect that."""
    eid = uuid4()
    other_id = uuid4()
    cooc = {
        'entity_id_1': str(eid),
        'entity_id_2': str(other_id),
        'entity_1_name': 'Memex',
        'entity_1_type': None,
        'entity_2_name': 'SomeEntity',
        'entity_2_type': '',
        'cooccurrence_count': 5,
    }
    mock_api.get_entity_cooccurrences.return_value = [cooc]

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert data[0]['entity_name'] == 'SomeEntity'
    assert data[0]['entity_id'] == str(other_id)
    assert data[0]['count'] == 5


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_multiple(mock_api, mcp_client):
    """Multiple co-occurrences should all appear."""
    eid = uuid4()
    id1 = uuid4()
    id2 = uuid4()
    id3 = uuid4()
    coocs = [
        {
            'entity_id_1': str(eid),
            'entity_id_2': str(id1),
            'entity_1_name': 'Memex',
            'entity_1_type': 'Software',
            'entity_2_name': 'PostgreSQL',
            'entity_2_type': 'Technology',
            'cooccurrence_count': 10,
        },
        {
            'entity_id_1': str(eid),
            'entity_id_2': str(id2),
            'entity_1_name': 'Memex',
            'entity_1_type': 'Software',
            'entity_2_name': 'Domain Layer',
            'entity_2_type': 'Architecture',
            'cooccurrence_count': 7,
        },
        {
            'entity_id_1': str(id3),
            'entity_id_2': str(eid),
            'entity_1_name': 'FastAPI',
            'entity_1_type': 'Framework',
            'entity_2_name': 'Memex',
            'entity_2_type': 'Software',
            'cooccurrence_count': 4,
        },
    ]
    mock_api.get_entity_cooccurrences.return_value = coocs

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    assert len(data) == 3
    names = {c['entity_name'] for c in data}
    assert 'PostgreSQL' in names
    assert 'Domain Layer' in names
    assert 'FastAPI' in names
    counts = {c['count'] for c in data}
    assert 10 in counts
    assert 7 in counts
    assert 4 in counts


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_missing_name_fields(mock_api, mcp_client):
    """Gracefully handle legacy data without name/type fields."""
    eid = uuid4()
    other_id = uuid4()
    cooc = {
        'entity_id_1': str(eid),
        'entity_id_2': str(other_id),
        'cooccurrence_count': 2,
    }
    mock_api.get_entity_cooccurrences.return_value = [cooc]

    result = await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})
    data = parse_tool_result(result)

    # Should fall back to str(other_id) for entity_name
    assert data[0]['entity_name'] == str(other_id)
    assert data[0]['entity_id'] == str(other_id)
    assert data[0]['count'] == 2


# ── memex_get_memory_units (batch) ──


@pytest.mark.asyncio
async def test_get_memory_units_batch(mock_api, mcp_client):
    uid1 = uuid4()
    uid2 = uuid4()
    nid = uuid4()
    ts = dt.datetime(2025, 3, 1, 10, 0, tzinfo=dt.timezone.utc)

    unit1 = MemoryUnitDTO(
        id=uid1,
        text='Docker containers isolate processes.',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=nid,
        vault_id=uuid4(),
        metadata={'source': 'docs'},
        mentioned_at=ts,
    )
    unit2 = MemoryUnitDTO(
        id=uid2,
        text='Kubernetes orchestrates containers.',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=nid,
        vault_id=uuid4(),
        metadata={},
        mentioned_at=ts,
    )

    mock_api.get_memory_unit.side_effect = [unit1, unit2]

    result = await mcp_client.call_tool(
        'memex_get_memory_units', {'unit_ids': [str(uid1), str(uid2)]}
    )
    data = parse_tool_result(result)

    assert len(data) == 2
    texts = {u['text'] for u in data}
    assert 'Docker containers isolate processes.' in texts
    assert 'Kubernetes orchestrates containers.' in texts
    ids = {u['id'] for u in data}
    assert str(uid1) in ids
    assert str(uid2) in ids


@pytest.mark.asyncio
async def test_get_memory_units_partial_failure(mock_api, mcp_client):
    """Batch should handle individual unit lookup failures gracefully (silently skip)."""
    uid1 = uuid4()
    uid2 = uuid4()
    nid = uuid4()

    unit1 = MemoryUnitDTO(
        id=uid1,
        text='Good unit.',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=nid,
        vault_id=uuid4(),
    )

    mock_api.get_memory_unit.side_effect = [unit1, RuntimeError('DB connection lost')]

    result = await mcp_client.call_tool(
        'memex_get_memory_units', {'unit_ids': [str(uid1), str(uid2)]}
    )
    data = parse_tool_result(result)

    # Only the successful unit is returned; error is silently dropped
    assert len(data) == 1
    assert data[0]['text'] == 'Good unit.'


@pytest.mark.asyncio
async def test_get_memory_units_invalid_uuid(mock_api, mcp_client):
    mock_api.get_memory_unit.return_value = None

    result = await mcp_client.call_tool(
        'memex_get_memory_units', {'unit_ids': ['invalid', str(uuid4())]}
    )
    data = parse_tool_result(result)
    # Invalid UUIDs are silently skipped; the valid UUID's unit returns None
    assert data == []


# ── memex_get_nodes (batch) ──


@pytest.mark.asyncio
async def test_get_nodes_batch(mock_api, mcp_client):
    """Batch node retrieval should return content for all found nodes."""
    from memex_common.schemas import NodeDTO

    nid1 = uuid4()
    nid2 = uuid4()
    note_id = uuid4()
    vault_id = uuid4()

    node1 = NodeDTO(
        id=nid1,
        note_id=note_id,
        vault_id=vault_id,
        title='Introduction',
        text='Hello world.',
        level=1,
        seq=0,
        status='active',
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
    )
    node2 = NodeDTO(
        id=nid2,
        note_id=note_id,
        vault_id=vault_id,
        title='Details',
        text='More content.',
        level=2,
        seq=1,
        status='active',
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
    )
    mock_api.get_nodes.return_value = [node1, node2]

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid1), str(nid2)]})
    data = parse_tool_result(result)

    assert len(data) == 2
    titles = {n['title'] for n in data}
    assert 'Introduction' in titles
    assert 'Details' in titles
    texts = {n['text'] for n in data}
    assert 'Hello world.' in texts
    assert 'More content.' in texts


@pytest.mark.asyncio
async def test_get_nodes_batch_single(mock_api, mcp_client):
    """Single node should work."""
    from memex_common.schemas import NodeDTO

    nid = uuid4()
    note_id = uuid4()
    vault_id = uuid4()

    node = NodeDTO(
        id=nid,
        note_id=note_id,
        vault_id=vault_id,
        title='Only Section',
        text='Single.',
        level=1,
        seq=0,
        status='active',
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
    )
    mock_api.get_nodes.return_value = [node]

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid)]})
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'Only Section'
    assert data[0]['text'] == 'Single.'


@pytest.mark.asyncio
async def test_get_nodes_batch_not_found(mock_api, mcp_client):
    """Not-found nodes are silently dropped; returns empty list."""
    nid = uuid4()
    mock_api.get_nodes.return_value = []

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid)]})
    data = parse_tool_result(result)

    assert data == []


@pytest.mark.asyncio
async def test_get_nodes_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid UUID'):
        await mcp_client.call_tool('memex_get_nodes', {'node_ids': ['not-valid']})


# ── memex_get_notes_metadata (batch) ──


@pytest.mark.asyncio
async def test_get_notes_metadata_batch(mock_api, mcp_client):
    """Batch metadata should return info for all valid notes."""
    nid1 = uuid4()
    nid2 = uuid4()

    mock_api.get_notes_metadata.return_value = [
        {
            'note_id': str(nid1),
            'title': 'Note One',
            'total_tokens': 200,
            'tags': ['python'],
            'has_assets': False,
        },
        {
            'note_id': str(nid2),
            'title': 'Note Two',
            'total_tokens': 800,
            'tags': [],
            'has_assets': True,
        },
    ]

    result = await mcp_client.call_tool(
        'memex_get_notes_metadata', {'note_ids': [str(nid1), str(nid2)]}
    )
    data = parse_tool_result(result)

    assert len(data) == 2
    titles = {n['title'] for n in data}
    assert 'Note One' in titles
    assert 'Note Two' in titles
    tokens = {n['total_tokens'] for n in data}
    assert 200 in tokens
    assert 800 in tokens
    assert any(n['has_assets'] for n in data)


@pytest.mark.asyncio
async def test_get_notes_metadata_batch_partial_failure(mock_api, mcp_client):
    """Should return only successful results; failures are silently dropped.
    Tests the fallback path: batch fails, individual calls handle partial success."""
    nid1 = uuid4()
    nid2 = uuid4()

    # Batch fails, triggering individual fallback
    mock_api.get_notes_metadata.side_effect = RuntimeError('batch unavailable')
    mock_api.get_note_metadata.side_effect = [
        {'title': 'Good Note', 'total_tokens': 100, 'tags': []},
        RuntimeError('Not found'),
    ]

    result = await mcp_client.call_tool(
        'memex_get_notes_metadata', {'note_ids': [str(nid1), str(nid2)]}
    )
    data = parse_tool_result(result)

    # Only the successful note is returned
    assert len(data) == 1
    assert data[0]['title'] == 'Good Note'


@pytest.mark.asyncio
async def test_get_notes_metadata_invalid_uuid(mock_api, mcp_client):
    with pytest.raises(ToolError, match='Invalid UUID'):
        await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': ['bad-uuid']})


# ── memex_memory_search includes note titles ──


@pytest.mark.asyncio
async def test_memory_search_includes_note_titles(mock_api, mcp_client):
    """Memory search results should include note titles."""
    nid = uuid4()
    uid = uuid4()

    unit = MemoryUnitDTO(
        id=uid,
        text='Important fact about architecture.',
        fact_type=FactTypes.WORLD,
        status='active',
        note_id=nid,
        vault_id=uuid4(),
    )
    mock_api.search.return_value = [unit]
    mock_api.get_notes_metadata.return_value = [
        {'note_id': str(nid), 'title': 'Architecture Guide'}
    ]

    result = await mcp_client.call_tool(
        'memex_memory_search', {'query': 'architecture', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['note_title'] == 'Architecture Guide'
    assert data[0]['note_id'] == str(nid)
