"""Unit tests for search-miss capture nudge (P6).

When memex_memory_search or memex_note_search returns zero results,
a system-hint should be returned instead of an empty list.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from memex_mcp.models import McpFact, McpNoteSearchResult

# Sentinel UUID used for hint facts
_SENTINEL_UUID = UUID(int=0)
_HINT_TEXT = (
    'No results found. If you learn something about this topic '
    'during this session, consider saving it.'
)


def _make_ctx():
    """Create a minimal mock context for tool handlers."""
    ctx = MagicMock()
    ctx.session_id = 'test-session'
    return ctx


@pytest.mark.asyncio
async def test_memory_search_empty_returns_hint():
    """Empty memory_search results → returns hint fact."""
    from memex_mcp.server import memex_memory_search

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[])

    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        results = await memex_memory_search(ctx=ctx, query='nonexistent topic')

    assert len(results) == 1
    hint = results[0]
    assert isinstance(hint, McpFact)
    assert hint.id == _SENTINEL_UUID
    assert hint.text == _HINT_TEXT
    assert hint.confidence == 0.0
    assert 'system-hint' in hint.tags


@pytest.mark.asyncio
async def test_note_search_empty_returns_hint():
    """Empty note_search results → returns hint note."""
    from memex_mcp.server import memex_note_search

    mock_api = AsyncMock()
    mock_api.search_notes = AsyncMock(return_value=[])

    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        results = await memex_note_search(ctx=ctx, query='nonexistent topic')

    assert len(results) == 1
    hint = results[0]
    assert isinstance(hint, McpNoteSearchResult)
    assert hint.note_id == _SENTINEL_UUID
    assert hint.title == 'No results'
    assert hint.score == 0.0
    assert _HINT_TEXT in (hint.description or '')
    assert 'system-hint' in hint.tags


@pytest.mark.asyncio
async def test_note_search_nonempty_no_hint():
    """Non-empty note_search results → no hint appended."""
    from memex_mcp.server import memex_note_search
    from memex_common.schemas import NoteSearchResult

    note_id = UUID('12345678-1234-1234-1234-123456789abc')
    mock_result = NoteSearchResult(
        note_id=note_id,
        metadata={'title': 'Real Note', 'name': 'Real Note'},
        summaries=[],
        score=0.85,
    )

    mock_api = AsyncMock()
    mock_api.search_notes = AsyncMock(return_value=[mock_result])

    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        results = await memex_note_search(ctx=ctx, query='real topic')

    assert len(results) == 1
    assert results[0].note_id != _SENTINEL_UUID
    assert 'system-hint' not in results[0].tags


@pytest.mark.asyncio
async def test_memory_search_nonempty_no_hint():
    """Non-empty memory_search results → no hint appended."""
    from memex_mcp.server import memex_memory_search

    mock_result = MagicMock()
    mock_result.id = UUID('12345678-1234-1234-1234-123456789abc')
    mock_result.text = 'A real fact'
    mock_result.fact_type = 'world'
    mock_result.score = 0.85
    mock_result.confidence = 0.9
    mock_result.note_id = UUID('abcdefab-abcd-abcd-abcd-abcdefabcdef')
    mock_result.node_ids = []
    mock_result.status = 'active'
    mock_result.superseded_by = []
    mock_result.metadata = {}
    mock_result.mentioned_at = None
    mock_result.occurred_start = None
    mock_result.occurred_end = None
    mock_result.event_date = None

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[mock_result])
    mock_api.get_notes_metadata = AsyncMock(return_value=[])

    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        results = await memex_memory_search(ctx=ctx, query='real topic')

    assert len(results) == 1
    assert results[0].id != _SENTINEL_UUID
    assert 'system-hint' not in results[0].tags
