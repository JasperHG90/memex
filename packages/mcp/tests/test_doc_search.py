"""Tests for the memex_note_search MCP tool and memex_read_note."""

from unittest.mock import MagicMock
from uuid import UUID, uuid4

TEST_VAULT_UUID = UUID('00000000-0000-0000-0000-000000000001')

import pytest
from fastmcp.exceptions import ToolError
from memex_common.schemas import BlockSummaryDTO, NoteSearchResult

from conftest import parse_tool_result


def _make_result(
    title: str | None = 'Test Document',
    score: float = 0.85,
    source_uri: str | None = None,
    answer: str | None = None,
    note_id: UUID | None = None,
    summaries: list[BlockSummaryDTO] | None = None,
) -> NoteSearchResult:
    metadata: dict = {}
    if title:
        metadata['title'] = title
        metadata['name'] = title
    if source_uri:
        metadata['source_uri'] = source_uri

    return NoteSearchResult(
        note_id=note_id or uuid4(),
        metadata=metadata,
        summaries=summaries or [],
        score=score,
        answer=answer,
    )


@pytest.mark.asyncio
async def test_memex_note_search_returns_formatted_results(mock_api, mcp_client):
    """Tool output should include document title, ID, and score."""
    doc = _make_result(title='My Research Paper', score=0.92)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'research', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]['title'] == 'My Research Paper'
    assert data[0]['note_id'] == str(doc.note_id)
    assert data[0]['score'] == pytest.approx(0.92, abs=0.001)


@pytest.mark.asyncio
async def test_memex_note_search_no_results(mock_api, mcp_client):
    """When no documents are found the tool returns an empty list."""
    mock_api.search_notes.return_value = []

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'unknown topic', 'vault_ids': ['test-vault']}
    )

    # Empty list may serialize as empty content or as '[]'
    if result.content:
        data = parse_tool_result(result)
        assert data == [] or data is None
    else:
        assert result.content == []


@pytest.mark.asyncio
async def test_memex_note_search_always_passes_summarize_false(mock_api, mcp_client):
    """MCP tool should always pass summarize=False to the API."""
    doc = _make_result(title='Any Doc')
    mock_api.search_notes.return_value = [doc]

    await mcp_client.call_tool('memex_note_search', {'query': 'test', 'vault_ids': ['test-vault']})

    mock_api.search_notes.assert_called_once()
    call_kwargs = mock_api.search_notes.call_args[1]
    assert call_kwargs['summarize'] is False


@pytest.mark.asyncio
async def test_memex_note_search_includes_source_uri(mock_api, mcp_client):
    """Source URI should appear in the output when present in document metadata."""
    doc = _make_result(title='Web Article', source_uri='https://example.com/article')
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'web content', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert data[0]['source_uri'] == 'https://example.com/article'


@pytest.mark.asyncio
async def test_memex_note_search_includes_summaries(mock_api, mcp_client):
    """Block summaries from core search result should appear in the output."""
    summaries = [
        BlockSummaryDTO(
            topic='Quarterly results analysis',
            key_points=['Statistical methods used', 'Q3 2025 data'],
        ),
    ]
    doc = _make_result(title='Rich Document', summaries=summaries)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'quarterly', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    result_summaries = data[0]['summaries']
    assert len(result_summaries) == 1
    assert result_summaries[0]['topic'] == 'Quarterly results analysis'
    assert 'Statistical methods used' in result_summaries[0]['key_points']


@pytest.mark.asyncio
async def test_memex_note_search_no_summaries_gives_empty_list(mock_api, mcp_client):
    """When no block summaries exist, summaries should be an empty list."""
    doc = _make_result(title='No Index Doc')
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'anything', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert data[0]['summaries'] == []


@pytest.mark.asyncio
async def test_memex_note_search_falls_back_to_name_key(mock_api, mcp_client):
    """When 'title' key is absent, fall back to 'name' key for document title."""
    doc = NoteSearchResult(
        note_id=uuid4(),
        metadata={'name': 'Named Document'},
        score=0.75,
    )
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'name fallback', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert data[0]['title'] == 'Named Document'


@pytest.mark.asyncio
async def test_memex_note_search_exception_handling(mock_api, mcp_client):
    """Tool must raise ToolError on failure."""
    mock_api.search_notes.side_effect = RuntimeError('DB connection lost')

    with pytest.raises(ToolError, match='DB connection lost'):
        await mcp_client.call_tool(
            'memex_note_search', {'query': 'crash test', 'vault_ids': ['test-vault']}
        )


@pytest.mark.asyncio
async def test_memex_note_search_no_tip_text(mock_api, mcp_client):
    """Structured results should not contain tip text about other tools."""
    doc = _make_result(title='Any Doc')
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'anything', 'vault_ids': ['test-vault']}
    )
    text = result.content[0].text

    assert 'memex_get_notes_metadata' not in text
    assert 'memex_get_page_indices' not in text


# --- memex_read_note force parameter tests ---


@pytest.mark.asyncio
async def test_memex_read_note_force_bypasses_token_limit(mock_api, mcp_client):
    """force=True should return full content even for >500 token notes."""
    note_id = str(uuid4())
    mock_api.get_note_metadata.return_value = {'total_tokens': 1000}
    mock_api.get_note.return_value = MagicMock(
        id=note_id,
        vault_id=str(TEST_VAULT_UUID),
        title='Big Note',
        doc_metadata={'name': 'Big Note'},
        original_text='Content of the note',
        created_at='2025-01-01',
        assets=[],
    )
    result = await mcp_client.call_tool('memex_read_note', {'note_id': note_id, 'force': True})
    data = parse_tool_result(result)

    assert data['title'] == 'Big Note'
    assert data['content'] == 'Content of the note'


@pytest.mark.asyncio
async def test_memex_read_note_without_force_blocks_large_note(mock_api, mcp_client):
    """Without force, >500 token notes should raise ToolError."""
    note_id = str(uuid4())
    mock_api.get_note_metadata.return_value = {'total_tokens': 1000}

    with pytest.raises(ToolError, match='tokens'):
        await mcp_client.call_tool('memex_read_note', {'note_id': note_id})


@pytest.mark.asyncio
async def test_memex_read_note_force_false_blocks_large_note(mock_api, mcp_client):
    """Explicit force=False should still block large notes."""
    note_id = str(uuid4())
    mock_api.get_note_metadata.return_value = {'total_tokens': 1000}

    with pytest.raises(ToolError, match='tokens'):
        await mcp_client.call_tool('memex_read_note', {'note_id': note_id, 'force': False})


@pytest.mark.asyncio
async def test_memex_read_note_small_note_no_force_needed(mock_api, mcp_client):
    """Notes under 500 tokens should work without force."""
    note_id = str(uuid4())
    mock_api.get_note_metadata.return_value = {'total_tokens': 200}
    mock_api.get_note.return_value = MagicMock(
        id=note_id,
        vault_id=str(TEST_VAULT_UUID),
        title='Small Note',
        doc_metadata={'name': 'Small Note'},
        original_text='Short content',
        created_at='2025-01-01',
        assets=[],
    )
    result = await mcp_client.call_tool('memex_read_note', {'note_id': note_id})
    data = parse_tool_result(result)

    assert data['title'] == 'Small Note'
    assert data['content'] == 'Short content'
