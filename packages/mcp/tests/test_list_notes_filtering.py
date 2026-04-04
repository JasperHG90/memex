"""Tests for memex_list_notes tag and status filtering (Task #5)."""

import datetime as dt

import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from fastmcp import Client
from memex_mcp.server import mcp
from conftest import parse_tool_result
from memex_common.schemas import NoteDTO


@pytest.fixture
def _note_factory():
    """Create NoteDTO instances with optional tags and status."""

    def _make(
        title: str = 'Test Note',
        tags: list[str] | None = None,
    ) -> NoteDTO:
        return NoteDTO(
            id=uuid4(),
            title=title,
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            vault_id=uuid4(),
            doc_metadata={'tags': tags or []},
        )

    return _make


@pytest.mark.asyncio
async def test_list_notes_with_tags_filter(mock_api, _note_factory):
    """memex_list_notes passes tags parameter to api.list_notes."""
    mock_api.list_notes = AsyncMock(
        return_value=[_note_factory(title='Tagged Note', tags=['python', 'ai'])]
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_list_notes',
            {'vault_id': 'test-vault', 'tags': ['python', 'ai']},
        )
        data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'Tagged Note'
    # Verify tags were passed through to the API
    call_kwargs = mock_api.list_notes.call_args.kwargs
    assert call_kwargs['tags'] == ['python', 'ai']


@pytest.mark.asyncio
async def test_list_notes_with_status_filter(mock_api, _note_factory):
    """memex_list_notes passes status parameter to api.list_notes."""
    mock_api.list_notes = AsyncMock(return_value=[_note_factory(title='Archived Note')])

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_list_notes',
            {'vault_id': 'test-vault', 'status': 'archived'},
        )
        data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'Archived Note'
    call_kwargs = mock_api.list_notes.call_args.kwargs
    assert call_kwargs['status'] == 'archived'


@pytest.mark.asyncio
async def test_list_notes_with_tags_and_status(mock_api, _note_factory):
    """memex_list_notes passes both tags and status parameters."""
    mock_api.list_notes = AsyncMock(
        return_value=[_note_factory(title='Filtered Note', tags=['devops'])]
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_list_notes',
            {'vault_id': 'test-vault', 'tags': ['devops'], 'status': 'active'},
        )
        data = parse_tool_result(result)

    assert len(data) == 1
    assert data[0]['title'] == 'Filtered Note'
    call_kwargs = mock_api.list_notes.call_args.kwargs
    assert call_kwargs['tags'] == ['devops']
    assert call_kwargs['status'] == 'active'


@pytest.mark.asyncio
async def test_list_notes_without_filters_backward_compatible(mock_api, _note_factory):
    """memex_list_notes without tags/status passes None for both (backward compatible)."""
    mock_api.list_notes = AsyncMock(return_value=[_note_factory(title='Any Note')])

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_list_notes',
            {'vault_id': 'test-vault'},
        )
        data = parse_tool_result(result)

    assert len(data) == 1
    call_kwargs = mock_api.list_notes.call_args.kwargs
    assert call_kwargs['tags'] is None
    assert call_kwargs['status'] is None


@pytest.mark.asyncio
async def test_list_notes_empty_result_with_tags(mock_api):
    """memex_list_notes returns empty list when no notes match tag filter."""
    mock_api.list_notes = AsyncMock(return_value=[])

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_list_notes',
            {'vault_id': 'test-vault', 'tags': ['nonexistent']},
        )
        data = parse_tool_result(result)

    assert data == [] or data is None
