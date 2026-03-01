"""Tests for the memex_note_search MCP tool."""

from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError
from memex_common.schemas import NoteSearchResult, NoteSnippet


def _make_result(
    title: str | None = 'Test Document',
    score: float = 0.85,
    source_uri: str | None = None,
    snippets: list[NoteSnippet] | None = None,
    answer: str | None = None,
) -> NoteSearchResult:
    metadata: dict = {}
    if title:
        metadata['title'] = title
        metadata['name'] = title
    if source_uri:
        metadata['source_uri'] = source_uri

    return NoteSearchResult(
        note_id=uuid4(),
        metadata=metadata,
        snippets=snippets or [],
        score=score,
        answer=answer,
    )


@pytest.mark.asyncio
async def test_memex_note_search_returns_formatted_results(mock_api, mcp_client):
    """Tool output should include document title, ID, and score."""
    doc = _make_result(title='My Research Paper', score=0.92)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'research'})
    text = result.content[0].text

    assert 'My Research Paper' in text
    assert str(doc.note_id) in text
    assert '0.920' in text
    assert "Found 1 note(s) for 'research'" in text


@pytest.mark.asyncio
async def test_memex_note_search_no_results(mock_api, mcp_client):
    """When no documents are found the tool returns a helpful message."""
    mock_api.search_notes.return_value = []

    result = await mcp_client.call_tool('memex_note_search', {'query': 'unknown topic'})
    text = result.content[0].text

    assert 'No notes found' in text
    assert 'unknown topic' in text


@pytest.mark.asyncio
async def test_memex_note_search_always_passes_summarize_false(mock_api, mcp_client):
    """MCP tool should always pass summarize=False to the API."""
    doc = _make_result(title='Any Doc')
    mock_api.search_notes.return_value = [doc]

    await mcp_client.call_tool('memex_note_search', {'query': 'test'})

    mock_api.search_notes.assert_called_once()
    call_kwargs = mock_api.search_notes.call_args[1]
    assert call_kwargs['summarize'] is False


@pytest.mark.asyncio
async def test_memex_note_search_includes_source_uri(mock_api, mcp_client):
    """Source URI should appear in the output when present in document metadata."""
    doc = _make_result(title='Web Article', source_uri='https://example.com/article')
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'web content'})
    text = result.content[0].text

    assert 'https://example.com/article' in text


@pytest.mark.asyncio
async def test_memex_note_search_includes_snippets(mock_api, mcp_client):
    """Up to 2 text snippets should appear in the output."""
    snippets = [
        NoteSnippet(text='First relevant passage.', score=0.9),
        NoteSnippet(text='Second relevant passage.', score=0.8),
        NoteSnippet(text='Third passage should be omitted.', score=0.7),
        NoteSnippet(text='Fourth passage should be omitted.', score=0.6),
    ]
    doc = _make_result(title='Rich Document', snippets=snippets)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'passages'})
    text = result.content[0].text

    assert 'First relevant passage.' in text
    assert 'Second relevant passage.' in text
    assert 'Third passage should be omitted.' not in text
    assert 'Fourth passage should be omitted.' not in text


@pytest.mark.asyncio
async def test_memex_note_search_snippet_node_title_prefix(mock_api, mcp_client):
    """Snippets with a node_title should display the title as a prefix."""
    snippets = [NoteSnippet(text='Section content.', score=0.9, node_title='Introduction')]
    doc = _make_result(title='Structured Doc', snippets=snippets)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'section'})
    text = result.content[0].text

    assert '[Introduction]' in text
    assert 'Section content.' in text


@pytest.mark.asyncio
async def test_memex_note_search_falls_back_to_name_key(mock_api, mcp_client):
    """When 'title' key is absent, fall back to 'name' key for document title."""
    doc = NoteSearchResult(
        note_id=uuid4(),
        metadata={'name': 'Named Document'},
        snippets=[],
        score=0.75,
    )
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'name fallback'})
    text = result.content[0].text

    assert 'Named Document' in text


@pytest.mark.asyncio
async def test_memex_note_search_exception_handling(mock_api, mcp_client):
    """Tool must raise ToolError on failure."""
    mock_api.search_notes.side_effect = RuntimeError('DB connection lost')

    with pytest.raises(ToolError, match='DB connection lost'):
        await mcp_client.call_tool('memex_note_search', {'query': 'crash test'})


@pytest.mark.asyncio
async def test_memex_note_search_tip_always_present(mock_api, mcp_client):
    """The follow-up tip should appear for every successful result."""
    doc = _make_result(title='Any Doc')
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool('memex_note_search', {'query': 'anything'})
    text = result.content[0].text

    assert 'memex_get_page_index' in text
    assert 'memex_get_node' in text
