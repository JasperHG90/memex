"""Tests for the memex_note_search MCP tool and memex_read_note."""

from unittest.mock import MagicMock
from uuid import UUID, uuid4

TEST_VAULT_UUID = UUID('00000000-0000-0000-0000-000000000001')

import pytest
from fastmcp.exceptions import ToolError
from memex_common.schemas import BlockSummaryDTO, MemoryLinkDTO, NoteSearchResult

from conftest import parse_tool_result


def _make_result(
    title: str | None = 'Test Document',
    score: float = 0.85,
    source_uri: str | None = None,
    answer: str | None = None,
    note_id: UUID | None = None,
    summaries: list[BlockSummaryDTO] | None = None,
    has_assets: bool = False,
    links: list[MemoryLinkDTO] | None = None,
) -> NoteSearchResult:
    metadata: dict = {}
    if title:
        metadata['title'] = title
        metadata['name'] = title
    if source_uri:
        metadata['source_uri'] = source_uri
    metadata['has_assets'] = has_assets

    return NoteSearchResult(
        note_id=note_id or uuid4(),
        metadata=metadata,
        summaries=summaries or [],
        score=score,
        answer=answer,
        links=links or [],
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
    """When no documents are found the tool returns a system-hint nudge."""
    mock_api.search_notes.return_value = []

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'unknown topic', 'vault_ids': ['test-vault']}
    )

    data = parse_tool_result(result)
    assert isinstance(data, list)
    assert len(data) == 1
    hint = data[0]
    assert hint['note_id'] == '00000000-0000-0000-0000-000000000000'
    assert hint['title'] == 'No results'
    assert 'system-hint' in hint['tags']


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
async def test_memex_note_search_multiple_summaries(mock_api, mcp_client):
    """Multiple block summaries should all be serialized into the result."""
    summaries = [
        BlockSummaryDTO(topic='Introduction', key_points=['Background']),
        BlockSummaryDTO(topic='Methods', key_points=['Approach A', 'Approach B']),
        BlockSummaryDTO(topic='Conclusion', key_points=[]),
    ]
    doc = _make_result(title='Multi-block Doc', summaries=summaries)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'multi', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    result_summaries = data[0]['summaries']
    assert len(result_summaries) == 3
    assert result_summaries[0]['topic'] == 'Introduction'
    assert result_summaries[1]['topic'] == 'Methods'
    assert result_summaries[1]['key_points'] == ['Approach A', 'Approach B']
    assert result_summaries[2]['topic'] == 'Conclusion'
    assert result_summaries[2]['key_points'] == []


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


# --- has_assets filter tests ---


@pytest.mark.asyncio
async def test_memex_note_search_has_assets_filter(mock_api, mcp_client):
    """has_assets=True should only return notes with assets."""
    with_assets = _make_result(title='Has Images', has_assets=True, note_id=uuid4())
    without_assets = _make_result(title='No Images', has_assets=False, note_id=uuid4())
    mock_api.search_notes.return_value = [with_assets, without_assets]

    result = await mcp_client.call_tool(
        'memex_note_search',
        {'query': 'architecture', 'vault_ids': ['test-vault'], 'has_assets': True},
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'Has Images'
    assert data[0]['has_assets'] is True


@pytest.mark.asyncio
async def test_memex_note_search_has_assets_false_returns_all(mock_api, mcp_client):
    """Default has_assets=False should return all notes regardless of assets."""
    with_assets = _make_result(title='Has Images', has_assets=True, note_id=uuid4())
    without_assets = _make_result(title='No Images', has_assets=False, note_id=uuid4())
    mock_api.search_notes.return_value = [with_assets, without_assets]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'architecture', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert len(data) == 2


@pytest.mark.asyncio
async def test_memex_note_search_has_assets_overfetches(mock_api, mcp_client):
    """has_assets=True should request 3x limit from core to compensate for filtering."""
    mock_api.search_notes.return_value = []

    await mcp_client.call_tool(
        'memex_note_search',
        {'query': 'diagrams', 'vault_ids': ['test-vault'], 'has_assets': True, 'limit': 5},
    )

    call_kwargs = mock_api.search_notes.call_args[1]
    assert call_kwargs['limit'] == 15


# --- link truncation tests ---


@pytest.mark.asyncio
async def test_memex_note_search_links_exclude_self(mock_api, mcp_client):
    """Self-referential links should be removed from results."""
    note_id = uuid4()
    other_id = uuid4()
    links = [
        MemoryLinkDTO(unit_id=uuid4(), note_id=note_id, relation='temporal', weight=1.0),
        MemoryLinkDTO(unit_id=uuid4(), note_id=other_id, relation='semantic', weight=0.8),
    ]
    doc = _make_result(title='Self Link Doc', note_id=note_id, links=links)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'test', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    result_links = data[0]['links']
    assert len(result_links) == 1
    assert result_links[0]['note_id'] == str(other_id)


@pytest.mark.asyncio
async def test_memex_note_search_links_top_5(mock_api, mcp_client):
    """Only the top 5 links by weight should be kept."""
    note_id = uuid4()
    links = [
        MemoryLinkDTO(unit_id=uuid4(), note_id=uuid4(), relation='semantic', weight=0.1 * (i + 1))
        for i in range(10)
    ]
    doc = _make_result(title='Many Links', note_id=note_id, links=links)
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'test', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    result_links = data[0]['links']
    assert len(result_links) == 5
    weights = [lnk['weight'] for lnk in result_links]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] == pytest.approx(1.0, abs=0.01)


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


# --- memex_note_search relation mapping tests ---


@pytest.mark.asyncio
async def test_memex_note_search_maps_related_notes_and_links(mock_api, mcp_client):
    """Verify memex_note_search maps related_notes and links from NoteSearchResult to MCP models."""
    from memex_common.schemas import MemoryLinkDTO, RelatedNoteDTO

    nid = uuid4()
    rn_id = uuid4()
    uid = uuid4()

    doc = NoteSearchResult(
        note_id=nid,
        metadata={'title': 'Test Doc'},
        score=0.85,
        related_notes=[
            RelatedNoteDTO(note_id=rn_id, title='Related', shared_entities=['Python'], strength=0.9)
        ],
        links=[
            MemoryLinkDTO(
                unit_id=uid, note_id=rn_id, note_title='Related', relation='semantic', weight=0.8
            )
        ],
    )
    mock_api.search_notes.return_value = [doc]

    result = await mcp_client.call_tool(
        'memex_note_search', {'query': 'test', 'vault_ids': ['test-vault']}
    )
    data = parse_tool_result(result)

    assert len(data) == 1
    assert len(data[0]['related_notes']) == 1
    assert data[0]['related_notes'][0]['note_id'] == str(rn_id)
    assert data[0]['related_notes'][0]['title'] == 'Related'
    assert data[0]['related_notes'][0]['shared_entities'] == ['Python']
    assert data[0]['related_notes'][0]['strength'] == 0.9
    assert len(data[0]['links']) == 1
    assert data[0]['links'][0]['unit_id'] == str(uid)
    assert data[0]['links'][0]['relation'] == 'semantic'
    assert data[0]['links'][0]['weight'] == 0.8
