import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime
from memex_dashboard.pages.overview import OverviewState
from memex_common.schemas import SystemStatsCountsDTO, NoteDTO, TokenUsageResponse


@pytest.mark.asyncio
async def test_fetch_metrics():
    state = OverviewState()

    # Mock API Client
    mock_api = AsyncMock()

    # 1. get_stats_counts
    mock_api.get_stats_counts.return_value = SystemStatsCountsDTO(
        memories=2, entities=3, reflection_queue=0, graph_edges=0
    )

    # 2. get_recent_notes
    doc1 = MagicMock(spec=NoteDTO)
    doc1.id = 'doc1'
    doc1.doc_metadata = {'title': 'Doc One'}
    doc1.created_at = datetime(2023, 1, 1)

    doc2 = MagicMock(spec=NoteDTO)
    doc2.id = 'doc2'
    doc2.doc_metadata = {}
    doc2.created_at = datetime(2023, 1, 2)

    mock_api.get_recent_notes.return_value = [doc1, doc2]

    # 3. get_token_usage
    mock_api.get_token_usage.return_value = TokenUsageResponse(usage=[])

    # Patch the api_client.api property
    with patch('memex_dashboard.pages.overview.api_client') as mock_client_module:
        mock_client_module.api = mock_api

        await state.fetch_db_stats()

        # Check metrics
        assert state.metrics['total_memories'] == 2
        assert state.metrics['total_entities'] == 3

        # Check recent memories
        assert len(state.recent_memories) == 2
        assert state.recent_memories[0]['title'] == 'Doc One'
        # Fallback logic check
        assert state.recent_memories[1]['title'] == 'Document doc2'

        # Check graph
        assert state.token_usage_graph is not None
