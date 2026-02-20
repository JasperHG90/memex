import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from memex_dashboard.pages.entity import EntityState
from memex_common.schemas import EntityDTO, SystemStatsCountsDTO


@pytest.mark.asyncio
async def test_filter_graph_state():
    state = EntityState()

    # Mock data
    ent_a = MagicMock(spec=EntityDTO)
    ent_a.id = 'a'
    ent_a.name = 'Entity A'
    ent_a.mention_count = 10  # High importance

    ent_b = MagicMock(spec=EntityDTO)
    ent_b.id = 'b'
    ent_b.name = 'Entity B'
    ent_b.mention_count = 5  # Medium importance

    ent_c = MagicMock(spec=EntityDTO)
    ent_c.id = 'c'
    ent_c.name = 'Entity C'
    ent_c.mention_count = 1  # Low importance

    # Mock API responses
    mock_api = MagicMock()
    mock_api.get_stats_counts = AsyncMock(
        return_value=SystemStatsCountsDTO(memories=100, entities=3, reflection_queue=0)
    )

    async def mock_list_gen(*args, **kwargs):
        for e in [ent_a, ent_b, ent_c]:
            yield e

    mock_api.list_entities_ranked = MagicMock(side_effect=mock_list_gen)

    # Mock co-occurrences (Edges)
    # A-B (Strong), B-C (Weak)
    mock_api._get = AsyncMock(
        return_value=[
            {'entity_id_1': 'a', 'entity_id_2': 'b', 'cooccurrence_count': 5, 'vault_id': None},
            {'entity_id_1': 'b', 'entity_id_2': 'c', 'cooccurrence_count': 1, 'vault_id': None},
        ]
    )

    with patch('memex_dashboard.pages.entity.api_client') as mock_client_module:
        mock_client_module.api = mock_api

        # 1. Load initial graph
        await state.on_load()
        assert len(state.nodes) == 3
        assert len(state.edges) == 2

        # 2. Filter by Node Importance (Exclude C)
        state.set_min_node_importance([5.0])
        state.apply_filters()

        # Should keep A (10) and B (5), remove C (1)
        # Edge B-C should also disappear because C is gone

        # Manually trigger filter application if it's not automatic (it should be in the real impl)
        # ideally set_min_node_importance triggers apply_filters

        assert len(state.nodes) == 2
        assert state.nodes[0].id in ['a', 'b']
        assert state.nodes[1].id in ['a', 'b']
        assert len(state.edges) == 1  # Only A-B remains

        # 3. Filter by Connection Strength (Exclude weak edges)
        state.set_min_node_importance([0.0])  # Reset node filter
        state.set_min_connection_strength([3.0])
        state.apply_filters()

        # Nodes: A, B, C (all present)
        # Edges: A-B (5) kept, B-C (1) removed
        assert len(state.nodes) == 3
        assert len(state.edges) == 1
        assert state.edges[0].u == 'a' and state.edges[0].v == 'b'
