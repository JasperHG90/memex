"""Tests for reference_date threading from MCP tools to the API."""

import pytest
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_memory_search_passes_reference_date(mock_api, mcp_client):
    """memex_memory_search should forward reference_date to api.search()."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search',
        {
            'query': 'what happened last week',
            'vault_ids': ['test-vault'],
            'reference_date': '2025-06-15T12:00:00',
        },
    )

    call_args = mock_api.search.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs['reference_date'] == datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_memory_search_omits_reference_date_when_none(mock_api, mcp_client):
    """reference_date should default to None when not provided."""
    mock_api.search.return_value = []

    await mcp_client.call_tool(
        'memex_memory_search',
        {'query': 'anything', 'vault_ids': ['test-vault']},
    )

    call_args = mock_api.search.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs.get('reference_date') is None


@pytest.mark.asyncio
async def test_note_search_passes_reference_date(mock_api, mcp_client):
    """memex_note_search should forward reference_date to api.search_notes()."""
    mock_api.search_notes.return_value = []

    await mcp_client.call_tool(
        'memex_note_search',
        {
            'query': 'meetings from last month',
            'vault_ids': ['test-vault'],
            'reference_date': '2025-03-01T00:00:00',
        },
    )

    call_args = mock_api.search_notes.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs['reference_date'] == datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_note_search_omits_reference_date_when_none(mock_api, mcp_client):
    """reference_date should default to None when not provided to note search."""
    mock_api.search_notes.return_value = []

    await mcp_client.call_tool(
        'memex_note_search',
        {'query': 'anything', 'vault_ids': ['test-vault']},
    )

    call_args = mock_api.search_notes.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs.get('reference_date') is None


def _empty_survey_result():
    """Build a SurveyResult-shaped object with no topics so the MCP layer
    can serialise it without going through real DB operations."""
    from unittest.mock import MagicMock

    result = MagicMock()
    result.query = 'q'
    result.sub_queries = []
    result.topics = []
    result.total_notes = 0
    result.total_facts = 0
    result.truncated = False
    return result


@pytest.mark.asyncio
async def test_survey_passes_after_before_reference_date(mock_api, mcp_client):
    """memex_survey should forward after/before/reference_date to api.survey()."""
    mock_api.survey.return_value = _empty_survey_result()

    await mcp_client.call_tool(
        'memex_survey',
        {
            'query': 'what was happening two weeks ago',
            'vault_ids': ['test-vault'],
            'after': '2025-01-01',
            'before': '2025-01-31',
            'reference_date': '2025-02-01T00:00:00',
        },
    )

    call_args = mock_api.survey.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs['after'] == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert kwargs['before'] == datetime(2025, 1, 31, tzinfo=timezone.utc)
    assert kwargs['reference_date'] == datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_survey_omits_dates_when_none(mock_api, mcp_client):
    """All three temporal params should default to None when not provided."""
    mock_api.survey.return_value = _empty_survey_result()

    await mcp_client.call_tool(
        'memex_survey',
        {'query': 'broad topic', 'vault_ids': ['test-vault']},
    )

    call_args = mock_api.survey.call_args
    assert call_args is not None
    kwargs = call_args.kwargs
    assert kwargs.get('after') is None
    assert kwargs.get('before') is None
    assert kwargs.get('reference_date') is None
