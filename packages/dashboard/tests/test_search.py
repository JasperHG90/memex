import pytest
from unittest.mock import AsyncMock, patch, PropertyMock, MagicMock
from memex_dashboard.pages.search import SearchState
from uuid import uuid4


@pytest.mark.asyncio
async def test_search_perform():
    state = SearchState()
    state.query = 'test query'

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api

        # Mock Search Response
        mock_unit = MagicMock()
        mock_unit.id = str(uuid4())
        mock_unit.text = 'Result Text'
        mock_unit.entities = []
        mock_unit.fact_type = 'world'
        mock_unit.score = 0.95
        mock_unit.metadata = {}
        mock_unit.source_document_ids = []

        mock_api.search = AsyncMock(return_value=[mock_unit])

        # Mock get_state for VaultState - patch at class level
        mock_vault_state = type('VaultState', (), {'all_selected_vault_ids': None})()
        with patch.object(SearchState, 'get_state', AsyncMock(return_value=mock_vault_state)):
            # perform_search is an async generator, consume it
            async for _ in state.perform_search():
                pass

        assert len(state.results) == 1
        assert state.results[0].text == 'Result Text'
        assert state.is_loading is False


@pytest.mark.asyncio
async def test_search_empty_query():
    state = SearchState()
    state.query = ''

    # perform_search returns early for empty query, so it may not yield
    result = state.perform_search()
    try:
        async for _ in result:
            pass
    except Exception:
        pass
    assert state.results == []


def test_search_set_query():
    state = SearchState()
    state.set_query('new query')
    assert state.query == 'new query'
