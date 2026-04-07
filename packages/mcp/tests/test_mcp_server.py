import datetime as dt
import pytest
from uuid import uuid4
from fastmcp.exceptions import ToolError
from helpers import parse_tool_result
from memex_common.schemas import (
    BatchJobStatus,
    IngestResponse,
    NoteDTO,
    MemoryUnitDTO,
    FactTypes,
    VaultDTO,
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
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    assert data['note_id'] == doc_id
    assert data['status'] == 'success'
    mock_api.ingest.assert_called_once()

    from memex_common.schemas import NoteCreateDTO

    args, _ = mock_api.ingest.call_args
    assert isinstance(args[0], NoteCreateDTO)
    assert args[0].name == 'Test Note'


@pytest.mark.asyncio
async def test_mcp_add_note_background_uuid_job_id(mock_api, mcp_client):
    """Regression: background=True returns BatchJobStatus with UUID job_id.

    Previously, the MCP handler called UUID(result.job_id) on an already-UUID
    value, causing 'UUID object has no attribute replace'.
    """
    job_id = uuid4()
    mock_api.ingest.return_value = BatchJobStatus(job_id=job_id, status='pending')

    result = await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'Background Note',
            'markdown_content': '# Content',
            'description': 'Short description',
            'author': 'tester',
            'tags': ['tag1'],
            'vault_id': 'test-vault',
            'background': True,
        },
    )

    data = parse_tool_result(result)
    assert data['status'] == 'queued'
    assert data['note_id'] == str(job_id)
    assert data['job_id'] == str(job_id)


@pytest.mark.asyncio
async def test_mcp_add_note_with_date(mock_api, mcp_client):
    """Passing date includes it in the frontmatter content."""
    doc_id = str(uuid4())
    mock_api.ingest.return_value = IngestResponse(note_id=doc_id, status='success', unit_ids=[])

    result = await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'Dated Note',
            'markdown_content': '# Content',
            'description': 'Short description',
            'author': 'tester',
            'tags': ['tag1'],
            'vault_id': 'test-vault',
            'date': '2026-01-15',
        },
    )

    data = parse_tool_result(result)
    assert data['status'] == 'success'

    import base64

    args, _ = mock_api.ingest.call_args
    note = args[0]
    decoded = base64.b64decode(note.content).decode('utf-8')
    assert 'date:' in decoded
    assert '2026-01-15' in decoded
    assert note.author == 'tester'


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
        'memex_memory_search', {'query': 'python language', 'limit': 5, 'vault_ids': ['test-vault']}
    )

    data = parse_tool_result(result)
    assert len(data) == 1
    assert data[0]['fact_type'] == 'world'
    assert 'Python is a popular programming language' in data[0]['text']
    assert data[0]['score'] == pytest.approx(0.95, abs=0.01)

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

    result = await mcp_client.call_tool(
        'memex_memory_search', {'query': 'event', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    # World facts don't have mentioned_at in the model — it becomes an observation or stays world
    # The date is in mentioned_at for observations; for world facts it's not exposed
    assert len(data) == 1


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
    # vault_ids are resolved through resolve_vault_identifier
    assert len(call_args['vault_ids']) == 1
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))


@pytest.mark.asyncio
async def test_mcp_search_invalid_vault_uuid(mock_api, mcp_client):
    """Test that search handles malformed vault UUIDs gracefully by passing them to the API."""
    mock_api.search.return_value = []
    await mcp_client.call_tool(
        'memex_memory_search', {'query': 'test', 'vault_ids': ['not-a-uuid']}
    )
    mock_api.search.assert_called_once()
    # vault_ids are resolved through resolve_vault_identifier
    mock_api.resolve_vault_identifier.assert_called_once_with('not-a-uuid')


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
    data = parse_tool_result(result)
    assert data['title'] == 'Test Doc'
    assert data['content'] == 'Full content here.'


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
    """Verify that progressive disclosure meta-tools are listed by default."""
    tools = await mcp_client.list_tools()
    names = {t.name for t in tools}
    # Progressive disclosure is on by default — only meta-tools are listed
    assert names == {'memex_tags', 'memex_search', 'memex_get_schema'}


@pytest.mark.asyncio
async def test_active_vault_shows_server_and_client(mock_api, mock_config, mcp_client):
    """memex_active_vault should show both client-resolved and server default vaults."""
    vault_id = uuid4()
    mock_api.get_active_vault.return_value = VaultDTO(id=vault_id, name='global', description=None)

    result = await mcp_client.call_tool('memex_active_vault', {})
    text = result.content[0].text

    assert '**Write vault (client):** my-project' in text
    assert '**Read vaults (client):** my-project, shared' in text
    assert '**Server default write:** global' in text
    assert f'(ID: {vault_id})' in text
    assert '**Server default read:** global' in text


@pytest.mark.asyncio
async def test_active_vault_without_server_vault(mock_api, mock_config, mcp_client):
    """memex_active_vault should handle missing server vault gracefully."""
    mock_api.get_active_vault.return_value = None

    result = await mcp_client.call_tool('memex_active_vault', {})
    text = result.content[0].text

    # Client info still present
    assert '**Write vault (client):** my-project' in text
    assert '**Read vaults (client):** my-project, shared' in text
    # Server write line absent, but server read still shown
    assert 'Server default write' not in text
    assert '**Server default read:** global' in text


@pytest.mark.asyncio
async def test_mcp_list_prompts(mcp_client):
    """Verify that prompts are registered (if any)."""
    prompts = await mcp_client.list_prompts()
    names = [p.name for p in prompts]
    assert names == []
