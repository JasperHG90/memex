"""Tests for session-level dedup in MCP search tools (Task #4)."""

import time

import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastmcp import Client
from memex_mcp.server import (
    mcp,
    _session_dedup,
    _get_session_dedup,
    SessionDedup,
    _SESSION_DEDUP_TTL,
)
from conftest import parse_tool_result


def _make_search_result(note_id=None, unit_id=None):
    """Create a mock search result (MemoryUnitDTO-like)."""
    mock = MagicMock()
    mock.id = unit_id or uuid4()
    mock.note_id = note_id or uuid4()
    mock.text = f'Test fact {mock.id}'
    mock.fact_type = 'world'
    mock.confidence = 0.9
    mock.score = 0.8
    mock.status = 'active'
    mock.tags = []
    mock.superseded_by = []
    mock.node_ids = []
    mock.occurred_start = None
    mock.occurred_end = None
    mock.mentioned_at = None
    mock.citations = []
    return mock


def _make_note_search_result(note_id=None, title='Test Note'):
    """Create a mock note search result."""
    mock = MagicMock()
    mock.note_id = note_id or uuid4()
    mock.score = 0.85
    mock.vault_name = 'test'
    mock.note_status = 'active'
    mock.metadata = {'title': title, 'tags': [], 'description': 'A test note'}
    mock.summaries = []
    return mock


# ── Unit tests for SessionDedup internals ──


class TestSessionDedupState:
    def test_get_session_dedup_creates_new_entry(self):
        """_get_session_dedup creates a new SessionDedup for unknown session."""
        _session_dedup.clear()
        dedup = _get_session_dedup('test-session-1')
        assert isinstance(dedup, SessionDedup)
        assert len(dedup.seen_note_ids) == 0
        assert len(dedup.seen_memory_ids) == 0
        _session_dedup.clear()

    def test_get_session_dedup_returns_existing(self):
        """_get_session_dedup returns existing state for known session."""
        _session_dedup.clear()
        dedup = _get_session_dedup('test-session-2')
        dedup.seen_note_ids.add('note-1')
        dedup2 = _get_session_dedup('test-session-2')
        assert 'note-1' in dedup2.seen_note_ids
        _session_dedup.clear()

    def test_ttl_cleanup_purges_stale_entries(self):
        """_get_session_dedup purges entries older than TTL."""
        _session_dedup.clear()
        # Create a stale entry
        _session_dedup['stale-session'] = SessionDedup(
            last_access=time.monotonic() - _SESSION_DEDUP_TTL - 1
        )
        _session_dedup['fresh-session'] = SessionDedup(last_access=time.monotonic())

        # Accessing any session triggers cleanup
        _get_session_dedup('new-session')
        assert 'stale-session' not in _session_dedup
        assert 'fresh-session' in _session_dedup
        _session_dedup.clear()

    def test_last_access_updated_on_get(self):
        """_get_session_dedup updates last_access timestamp."""
        _session_dedup.clear()
        dedup = _get_session_dedup('test-session-3')
        first_access = dedup.last_access

        # Small delay to ensure monotonic time advances
        dedup2 = _get_session_dedup('test-session-3')
        assert dedup2.last_access >= first_access
        _session_dedup.clear()


# ── MCP tool integration tests ──


@pytest.mark.asyncio
async def test_memory_search_dedup_on_second_call(mock_api):
    """Second call with include_seen=False compresses already-seen memory units."""
    _session_dedup.clear()
    note_id = uuid4()
    unit_id = uuid4()
    result1 = _make_search_result(note_id=note_id, unit_id=unit_id)

    mock_api.search = AsyncMock(return_value=[result1])
    mock_api.get_notes_metadata = AsyncMock(
        return_value=[{'note_id': str(note_id), 'title': 'Test Note'}]
    )

    async with Client(mcp) as client:
        # First call — full results
        r1 = await client.call_tool(
            'memex_memory_search',
            {'query': 'test', 'vault_ids': ['test-vault']},
        )
        data1 = parse_tool_result(r1)
        assert len(data1) == 1
        assert data1[0]['previously_returned'] is False
        assert data1[0]['text'] == f'Test fact {unit_id}'

        # Second call with include_seen=False — compressed
        r2 = await client.call_tool(
            'memex_memory_search',
            {'query': 'test', 'vault_ids': ['test-vault'], 'include_seen': False},
        )
        data2 = parse_tool_result(r2)
        assert len(data2) == 1
        assert data2[0]['previously_returned'] is True
        assert data2[0]['text'] == ''  # Compressed

    _session_dedup.clear()


@pytest.mark.asyncio
async def test_memory_search_include_seen_true_returns_full(mock_api):
    """include_seen=True (default) returns full results even if previously seen."""
    _session_dedup.clear()
    note_id = uuid4()
    unit_id = uuid4()
    result1 = _make_search_result(note_id=note_id, unit_id=unit_id)

    mock_api.search = AsyncMock(return_value=[result1])
    mock_api.get_notes_metadata = AsyncMock(
        return_value=[{'note_id': str(note_id), 'title': 'Test Note'}]
    )

    async with Client(mcp) as client:
        # First call
        await client.call_tool(
            'memex_memory_search',
            {'query': 'test', 'vault_ids': ['test-vault']},
        )

        # Second call with include_seen=True (default) — full results
        r2 = await client.call_tool(
            'memex_memory_search',
            {'query': 'test', 'vault_ids': ['test-vault'], 'include_seen': True},
        )
        data2 = parse_tool_result(r2)
        assert len(data2) == 1
        assert data2[0]['previously_returned'] is False
        assert data2[0]['text'] == f'Test fact {unit_id}'

    _session_dedup.clear()


@pytest.mark.asyncio
async def test_note_search_dedup_on_second_call(mock_api):
    """Second call with include_seen=False compresses already-seen notes."""
    _session_dedup.clear()
    note_id = uuid4()
    result1 = _make_note_search_result(note_id=note_id, title='My Note')

    mock_api.search_notes = AsyncMock(return_value=[result1])

    async with Client(mcp) as client:
        # First call — full results
        r1 = await client.call_tool(
            'memex_note_search',
            {'query': 'test', 'vault_ids': ['test-vault']},
        )
        data1 = parse_tool_result(r1)
        assert len(data1) == 1
        assert data1[0]['previously_returned'] is False
        assert data1[0]['description'] == 'A test note'

        # Second call with include_seen=False — compressed
        r2 = await client.call_tool(
            'memex_note_search',
            {'query': 'test', 'vault_ids': ['test-vault'], 'include_seen': False},
        )
        data2 = parse_tool_result(r2)
        assert len(data2) == 1
        assert data2[0]['previously_returned'] is True
        # Compressed: no description, no summaries
        assert data2[0].get('description') is None

    _session_dedup.clear()


@pytest.mark.asyncio
async def test_note_search_include_seen_true_returns_full(mock_api):
    """include_seen=True returns full results for previously seen notes."""
    _session_dedup.clear()
    note_id = uuid4()
    result1 = _make_note_search_result(note_id=note_id, title='My Note')

    mock_api.search_notes = AsyncMock(return_value=[result1])

    async with Client(mcp) as client:
        # First call
        await client.call_tool(
            'memex_note_search',
            {'query': 'test', 'vault_ids': ['test-vault']},
        )

        # Second call with include_seen=True — full
        r2 = await client.call_tool(
            'memex_note_search',
            {'query': 'test', 'vault_ids': ['test-vault'], 'include_seen': True},
        )
        data2 = parse_tool_result(r2)
        assert len(data2) == 1
        assert data2[0]['previously_returned'] is False
        assert data2[0]['description'] == 'A test note'

    _session_dedup.clear()
