import datetime as dt
import pytest
from uuid import uuid4
from fastmcp.exceptions import ToolError
from memex_common.schemas import (
    IngestResponse,
    NoteDTO,
    MemoryUnitDTO,
    FactTypes,
)


@pytest.mark.asyncio
async def test_mcp_add_note_tool(mock_api, mcp_client):
    """Test the add note tool via the MCP Client, including schema validation."""
    doc_id = str(uuid4())
    mock_api.ingest.return_value = IngestResponse(note_id=doc_id, status='success', unit_ids=[])

    result = await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'Test Note',
            'markdown_content': '# Content',
            'description': 'Short description',
            'author': 'tester',
            'tags': ['tag1'],
        },
    )

    assert f'ID: {doc_id}' in result.content[0].text
    mock_api.ingest.assert_called_once()

    from memex_common.schemas import NoteCreateDTO

    args, _ = mock_api.ingest.call_args
    assert isinstance(args[0], NoteCreateDTO)
    assert args[0].name == 'Test Note'


@pytest.mark.asyncio
async def test_mcp_search_tool(mock_api, mcp_client):
    """Test the search tool via the MCP Client."""
    unit_id = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=unit_id,
            text='Python is a popular programming language.',
            fact_type=FactTypes.WORLD,
            score=0.95,
            vault_id=uuid4(),
            metadata={},
        )
    ]

    result = await mcp_client.call_tool(
        'memex_memory_search', {'query': 'python language', 'limit': 5}
    )

    assert 'Found 1 results' in result.content[0].text
    assert '[world]' in result.content[0].text
    assert 'Python is a popular programming language' in result.content[0].text
    assert '(0.95)' in result.content[0].text

    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['query'] == 'python language'
    assert call_args['limit'] == 5


@pytest.mark.asyncio
async def test_mcp_search_includes_date(mock_api, mcp_client):
    """Search results should include dates when available."""
    unit_id = uuid4()
    ts = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=unit_id,
            text='Event happened.',
            fact_type=FactTypes.WORLD,
            score=0.8,
            vault_id=uuid4(),
            metadata={},
            mentioned_at=ts,
        )
    ]

    result = await mcp_client.call_tool('memex_memory_search', {'query': 'event'})
    text = result.content[0].text

    assert '(2025-06-15' in text


@pytest.mark.asyncio
async def test_mcp_search_with_vault_filter(mock_api, mcp_client):
    """Test searching with a specific vault filter."""
    vault_id = uuid4()
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search', {'query': 'secret project', 'vault_ids': [str(vault_id)]}
    )

    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['vault_ids'] == [str(vault_id)]


@pytest.mark.asyncio
async def test_mcp_search_invalid_vault_uuid(mock_api, mcp_client):
    """Test that search handles malformed vault UUIDs gracefully by passing them to the API."""
    mock_api.search.return_value = []
    await mcp_client.call_tool(
        'memex_memory_search', {'query': 'test', 'vault_ids': ['not-a-uuid']}
    )
    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['vault_ids'] == ['not-a-uuid']


@pytest.mark.asyncio
async def test_mcp_read_note_success(mock_api, mcp_client):
    """Test reading a note successfully."""
    doc_id = uuid4()
    vault_id = uuid4()
    # Return metadata with < 500 tokens so the guard allows reading
    mock_api.get_note_metadata.return_value = {'title': 'Test Doc', 'total_tokens': 100}
    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Test Doc', 'description': 'Test Description'},
        original_text='Full content here.',
        vault_id=vault_id,
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    )

    result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
    assert '# Test Doc' in result.content[0].text
    assert 'Full content here.' in result.content[0].text


@pytest.mark.asyncio
async def test_mcp_read_note_blocked_large(mock_api, mcp_client):
    """Test that reading a large note (>= 500 tokens) raises ToolError."""
    doc_id = uuid4()
    mock_api.get_note_metadata.return_value = {'title': 'Big Note', 'total_tokens': 2000}

    with pytest.raises(ToolError, match='2000 tokens'):
        await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})


@pytest.mark.asyncio
async def test_mcp_read_note_not_found(mock_api, mcp_client):
    """Test reading a note that doesn't exist raises ToolError."""
    doc_id = uuid4()
    mock_api.get_note_metadata.return_value = None
    mock_api.get_note.side_effect = FileNotFoundError()

    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})

    assert 'not found' in str(excinfo.value)
    assert 'observation' in str(excinfo.value)


@pytest.mark.asyncio
async def test_mcp_list_tools(mcp_client):
    """Verify that all expected tools are registered."""
    tools = await mcp_client.list_tools()
    names = [t.name for t in tools]
    # Core tools should exist
    assert 'memex_add_note' in names
    assert 'memex_memory_search' in names
    assert 'memex_note_search' in names
    assert 'memex_list_vaults' in names
    assert 'memex_recent_notes' in names
    assert 'memex_list_entities' in names
    assert 'memex_get_entities' in names
    assert 'memex_get_entity_mentions' in names
    assert 'memex_get_entity_cooccurrences' in names
    assert 'memex_get_memory_units' in names
    assert 'memex_get_nodes' in names
    assert 'memex_get_notes_metadata' in names
    assert 'memex_get_page_indices' in names
    assert 'memex_get_resources' in names
    # Removed tools should not exist
    assert 'memex_get_page_index' not in names
    assert 'memex_get_resource' not in names
    assert 'memex_reflect' not in names
    assert 'memex_get_lineage' not in names
    assert 'memex_batch_ingest' not in names
    assert 'memex_get_batch_status' not in names
    assert 'memex_migrate_note' not in names
    assert 'memex_ingest_url' not in names
    assert 'memex_get_node' not in names
    assert 'memex_get_note_metadata' not in names
    assert 'memex_get_memory_unit' not in names
    assert 'memex_get_entity' not in names
    assert 'memex_list_notes' not in names


@pytest.mark.asyncio
async def test_mcp_list_prompts(mcp_client):
    """Verify that prompts are registered (if any)."""
    prompts = await mcp_client.list_prompts()
    names = [p.name for p in prompts]
    assert names == []
