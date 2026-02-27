import pytest
from unittest.mock import AsyncMock, patch, PropertyMock
from memex_dashboard.pages.lineage import LineageState
from uuid import uuid4


@pytest.mark.asyncio
async def test_lineage_data_transformation():
    state = LineageState()
    entity_id = uuid4()

    with patch('memex_dashboard.api.APIClient.api', new_callable=PropertyMock) as mock_api_prop:
        mock_api = AsyncMock()
        mock_api_prop.return_value = mock_api

        # Mock LineageResponse
        # Model -> Observation -> Unit -> Doc
        mock_api.get_entity_lineage = AsyncMock(
            return_value=AsyncMock(
                entity_type='mental_model',
                entity={'id': str(entity_id), 'name': 'Test Model'},
                derived_from=[
                    AsyncMock(
                        entity_type='observation',
                        entity={'id': str(uuid4()), 'statement': 'Test Observation'},
                        derived_from=[
                            AsyncMock(
                                entity_type='memory_unit',
                                entity={'id': str(uuid4()), 'text': 'Test Unit'},
                                derived_from=[
                                    AsyncMock(
                                        entity_type='note',
                                        entity={'id': str(uuid4()), 'name': 'Test Note'},
                                        derived_from=[],
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )
        )

        # We need to set a target entity to fetch lineage for
        state.target_id = str(entity_id)
        state.target_type = 'mental_model'

        # Mock get_top_entities for available_models in on_load
        mock_api.get_top_entities = AsyncMock(
            return_value=[AsyncMock(id=str(entity_id), name='Test Model')]
        )

        await state.on_load()

        assert len(state.nodes) >= 4
        assert len(state.edges) >= 3

        # Verify layers
        layers = [n.x for n in state.nodes]
        assert '20%' in layers  # Note
        assert '45%' in layers  # Unit
        assert '70%' in layers  # Observation
        assert '85%' in layers  # Model
