import pytest
from uuid import uuid4
from memex_common.schemas import (
    ReflectionResultDTO,
    ObservationDTO,
    MemoryUnitDTO,
    FactTypes,
)


@pytest.mark.asyncio
async def test_mcp_reflect_tool(mock_api, mcp_client):
    """Test the reflect tool via the MCP Client."""
    entity_id = uuid4()

    # Mock result from API using real DTOs
    mock_api.reflect_batch.return_value = [
        ReflectionResultDTO(
            entity_id=entity_id,
            new_observations=[ObservationDTO(title='Obs1', content='Content1')],
            status='success',
        )
    ]

    result = await mcp_client.call_tool('memex_reflect', {'entity_id': str(entity_id), 'limit': 10})

    assert 'Reflection complete' in result.content[0].text
    assert 'Obs1: Content1' in result.content[0].text
    mock_api.reflect_batch.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_add_note_tool(mock_api, mcp_client):
    """Test the add note tool via the MCP Client, including schema validation."""
    doc_id = str(uuid4())
    # API returns a dict, not an IngestResponse object
    mock_api.ingest.return_value = {'status': 'success', 'note_id': doc_id, 'unit_ids': []}

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

    # Verify it was called with NoteDTO
    from memex_common.schemas import NoteCreateDTO

    args, _ = mock_api.ingest.call_args
    assert isinstance(args[0], NoteCreateDTO)
    assert args[0].name == 'Test Note'


@pytest.mark.asyncio
async def test_mcp_search_tool(mock_api, mcp_client):
    """Test the search tool via the MCP Client."""
    # Mock search results
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

    result = await mcp_client.call_tool('memex_search', {'query': 'python language', 'limit': 5})

    assert 'Found 1 results' in result.content[0].text
    assert '[Type: world]' in result.content[0].text
    assert 'Python is a popular programming language' in result.content[0].text
    assert '(Score: 0.95)' in result.content[0].text

    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['query'] == 'python language'
    assert call_args['limit'] == 5


@pytest.mark.asyncio
async def test_mcp_search_with_vault_filter(mock_api, mcp_client):
    """Test searching with a specific vault filter."""
    vault_id = uuid4()
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_search', {'query': 'secret project', 'vault_ids': [str(vault_id)]}
    )

    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['vault_ids'] == [str(vault_id)]


@pytest.mark.asyncio
async def test_mcp_search_invalid_vault_uuid(mock_api, mcp_client):
    """Test that search handles malformed vault UUIDs gracefully by passing them to the API."""
    mock_api.search.return_value = []
    await mcp_client.call_tool('memex_search', {'query': 'test', 'vault_ids': ['not-a-uuid']})
    mock_api.search.assert_called_once()
    call_args = mock_api.search.call_args[1]
    assert call_args['vault_ids'] == ['not-a-uuid']


@pytest.mark.asyncio
async def test_mcp_read_note_success(mock_api, mcp_client):
    """Test reading a note successfully."""
    doc_id = uuid4()
    vault_id = uuid4()
    # API returns a dict, not a NoteDTO
    mock_api.get_note.return_value = {
        'id': doc_id,
        'doc_metadata': {'name': 'Test Doc', 'description': 'Test Description'},
        'original_text': 'Full content here.',
        'vault_id': vault_id,
        'created_at': '2024-01-01T00:00:00Z',
    }

    result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
    assert '# Test Doc' in result.content[0].text
    assert 'Full content here.' in result.content[0].text


@pytest.mark.asyncio
async def test_mcp_read_note_not_found(mock_api, mcp_client):
    """Test reading a note that doesn't exist returns the informative error."""
    doc_id = uuid4()
    mock_api.get_note.side_effect = FileNotFoundError()

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})

    assert 'not found' in str(excinfo.value)
    assert 'observation' in str(excinfo.value)


@pytest.mark.asyncio
async def test_mcp_list_tools(mcp_client):
    """Verify that all expected tools are registered."""
    tools = await mcp_client.list_tools()
    names = [t.name for t in tools]
    assert 'memex_reflect' in names
    assert 'memex_add_note' in names
    assert 'memex_search' in names
    assert 'memex_adjust_belief' not in names


@pytest.mark.asyncio
async def test_mcp_list_prompts(mcp_client):
    """Verify that prompts are registered (if any)."""
    prompts = await mcp_client.list_prompts()
    # FastMCP should auto-load from the prompts directory if configured
    # Currently, prompts are not auto-loaded or configured in server.py, so we expect empty list.
    names = [p.name for p in prompts]
    assert names == []
