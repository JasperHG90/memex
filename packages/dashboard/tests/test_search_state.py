"""Tests for SearchState summary features (toggle, generation, race condition guard)."""

import pytest
from unittest.mock import AsyncMock, patch, PropertyMock, MagicMock
from memex_dashboard.pages.search import SearchState, SearchResult
from memex_common.schemas import SummaryResponse
from uuid import uuid4


def _make_search_result(text: str = 'Result Text') -> SearchResult:
    """Helper to create a SearchResult for testing."""
    return SearchResult(
        id=str(uuid4()),
        text=text,
        fact_type='world',
        score=0.95,
        metadata={},
        source_document_ids=[],
    )


async def _consume_async_gen(gen):
    """Helper to consume an async generator."""
    async for _ in gen:
        pass


@pytest.mark.asyncio
async def test_generate_summary():
    """Verify generate_summary calls the API and sets summary_text."""
    state = SearchState()
    state.query = 'test query'
    state.results = [_make_search_result('first'), _make_search_result('second')]

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api
        mock_api.summarize = AsyncMock(return_value=SummaryResponse(summary='Summary [0] and [1].'))

        await _consume_async_gen(state.generate_summary())

    assert state.summary_text == 'Summary [0] and [1].'
    assert state.is_summary_loading is False


@pytest.mark.asyncio
async def test_generate_summary_race_condition_guard():
    """Verify that stale summary generation does not overwrite newer results."""
    state = SearchState()
    state.query = 'test query'
    state.results = [_make_search_result()]

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api

        # Simulate a slow API call where generation counter changes mid-flight
        async def slow_summarize(**kwargs):
            # Simulate another generation starting while this one runs
            state._summary_generation += 1
            return SummaryResponse(summary='Stale summary')

        mock_api.summarize = AsyncMock(side_effect=slow_summarize)

        await _consume_async_gen(state.generate_summary())

    # Stale result should NOT be written
    assert state.summary_text == ''


@pytest.mark.asyncio
async def test_generate_summary_failure():
    """Verify summary failure sets an error message."""
    state = SearchState()
    state.query = 'test query'
    state.results = [_make_search_result()]

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api
        mock_api.summarize = AsyncMock(side_effect=RuntimeError('API down'))

        await _consume_async_gen(state.generate_summary())

    assert 'failed' in state.summary_text.lower()
    assert state.is_summary_loading is False


@pytest.mark.asyncio
async def test_generate_summary_skips_when_already_loading():
    """Verify generate_summary is a no-op when already loading."""
    state = SearchState()
    state.query = 'test query'
    state.results = [_make_search_result()]
    state.is_summary_loading = True

    # Should return early without calling the API
    await _consume_async_gen(state.generate_summary())

    assert state.summary_text == ''


@pytest.mark.asyncio
async def test_generate_summary_skips_when_no_results():
    """Verify generate_summary is a no-op when there are no results."""
    state = SearchState()
    state.query = 'test query'
    state.results = []

    await _consume_async_gen(state.generate_summary())

    assert state.summary_text == ''


def test_toggle_summary_on_triggers_generation():
    """Verify toggling summary on returns generate_summary when results exist."""
    state = SearchState()
    state.results = [_make_search_result()]
    state.summary_text = ''

    result = state.toggle_summary(True)

    assert state.show_summary is True
    assert result == SearchState.generate_summary


def test_toggle_summary_on_no_results():
    """Verify toggling summary on with no results does not trigger generation."""
    state = SearchState()
    state.results = []
    state.summary_text = ''

    result = state.toggle_summary(True)

    assert state.show_summary is True
    assert result is None


def test_toggle_summary_off():
    """Verify toggling summary off does not trigger generation."""
    state = SearchState()
    state.show_summary = True
    state.results = [_make_search_result()]

    result = state.toggle_summary(False)

    assert state.show_summary is False
    assert result is None


def test_toggle_summary_on_with_existing_text():
    """Verify toggling on when summary already exists does not re-trigger."""
    state = SearchState()
    state.results = [_make_search_result()]
    state.summary_text = 'Already exists'

    result = state.toggle_summary(True)

    assert state.show_summary is True
    assert result is None


@pytest.mark.asyncio
async def test_perform_search_clears_summary():
    """Verify perform_search clears summary_text."""
    state = SearchState()
    state.query = 'test query'
    state.summary_text = 'old summary'

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api

        mock_unit = MagicMock()
        mock_unit.id = str(uuid4())
        mock_unit.text = 'Result Text'
        mock_unit.fact_type = 'world'
        mock_unit.score = 0.95
        mock_unit.metadata = {}
        mock_unit.source_document_ids = []

        mock_api.search = AsyncMock(return_value=[mock_unit])

        # Mock get_state for VaultState - patch at class level
        mock_vault_state = type('VaultState', (), {'all_selected_vault_ids': None})()
        with patch.object(SearchState, 'get_state', AsyncMock(return_value=mock_vault_state)):
            await _consume_async_gen(state.perform_search())

    assert state.summary_text == ''
    assert len(state.results) == 1


@pytest.mark.asyncio
async def test_perform_search_triggers_summary_when_toggle_on():
    """Verify perform_search returns generate_summary when show_summary is True."""
    state = SearchState()
    state.query = 'test query'
    state.show_summary = True

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api

        mock_unit = MagicMock()
        mock_unit.id = str(uuid4())
        mock_unit.text = 'Result Text'
        mock_unit.fact_type = 'world'
        mock_unit.score = 0.95
        mock_unit.metadata = {}
        mock_unit.source_document_ids = []

        mock_api.search = AsyncMock(return_value=[mock_unit])

        # Mock get_state for VaultState - patch at class level
        mock_vault_state = type('VaultState', (), {'all_selected_vault_ids': None})()
        with patch.object(SearchState, 'get_state', AsyncMock(return_value=mock_vault_state)):
            # perform_search is an async generator
            # The last yielded value should be generate_summary when show_summary is True
            last_result = None
            async for item in state.perform_search():
                last_result = item

    # perform_search should return the generate_summary event handler
    assert last_result == SearchState.generate_summary
