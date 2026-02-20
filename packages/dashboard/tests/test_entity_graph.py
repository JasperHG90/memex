import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from memex_dashboard.pages.entity import EntityState
from memex_common.schemas import EntityDTO, SystemStatsCountsDTO


@pytest.mark.asyncio
async def test_graph_generation():
    state = EntityState()

    # Mock entities
    ent_a = MagicMock(spec=EntityDTO)
    ent_a.id = 'a'
    ent_a.name = 'Entity A'
    ent_a.mention_count = 10

    ent_b = MagicMock(spec=EntityDTO)
    ent_b.id = 'b'
    ent_b.name = 'Entity B'
    ent_b.mention_count = 5

    ent_c = MagicMock(spec=EntityDTO)
    ent_c.id = 'c'
    ent_c.name = 'Entity C'
    ent_c.mention_count = 2

    # Mock API Client
    mock_api = MagicMock()  # Use MagicMock as base

    # 1. get_stats_counts
    mock_api.get_stats_counts = AsyncMock()
    mock_api.get_stats_counts.return_value = SystemStatsCountsDTO(
        memories=100, entities=3, reflection_queue=0
    )

    # 2. list_entities_ranked (async generator)
    async def mock_list_entities_gen(*args, **kwargs):
        for e in [ent_a, ent_b, ent_c]:
            yield e

    mock_api.list_entities_ranked = MagicMock(side_effect=mock_list_entities_gen)

    # 3. _get (used for bulk cooccurrences)
    mock_api._get = AsyncMock()
    mock_api._get.return_value = []

    with patch('memex_dashboard.pages.entity.api_client') as mock_client_module:
        mock_client_module.api = mock_api

        await state.on_load()

        # Check that graph is generated
        assert len(state.nodes) == 3
