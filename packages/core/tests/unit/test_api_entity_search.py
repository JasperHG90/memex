import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from memex_core.memory.sql_models import Entity


@pytest.mark.asyncio
async def test_search_entities(api, mock_metastore):
    read_session = mock_metastore.session.return_value.__aenter__.return_value

    # Mock entities — service now returns (Entity, MentalModel|None) tuples from LEFT JOIN
    e1 = Entity(id=uuid4(), canonical_name='Apple Inc.')
    e2 = Entity(id=uuid4(), canonical_name='Pineapple')

    mock_res = MagicMock()
    mock_res.all.return_value = [(e1, None), (e2, None)]
    read_session.exec.return_value = mock_res

    result = await api.search_entities(query='apple')

    assert len(result) == 2
    assert result[0].canonical_name == 'Apple Inc.'
    assert result[1].canonical_name == 'Pineapple'

    # Verify the query was called correctly
    read_session.exec.assert_called()


@pytest.mark.asyncio
async def test_server_entity_search(api, mock_metastore, mock_filestore):
    from fastapi.testclient import TestClient
    from memex_core.server import app
    from memex_core.server.common import get_api
    from unittest.mock import patch

    mock_config = MagicMock()
    mock_config.server.memory.extraction.model.model = 'test-model'
    mock_config.server.active_vault = 'global'
    mock_config.server.attached_vaults = []
    mock_config.server.logging.level = 'WARNING'
    mock_config.server.logging.json_output = False
    mock_config.server.host = '127.0.0.1'

    # Configure mock_metastore to support lifespan initialization
    mock_metastore.connect = AsyncMock()
    mock_metastore.close = AsyncMock()
    mock_metastore.session.return_value.__aenter__.return_value.get = AsyncMock(return_value=None)
    mock_metastore.session.return_value.__aenter__.return_value.commit = AsyncMock()

    # Override search_entities to return mock data
    e1 = Entity(id=uuid4(), canonical_name='Search Match')
    e1._mental_model_metadata = {}
    api.search_entities = AsyncMock(return_value=[e1])
    api.initialize = AsyncMock()  # Mock initialize to avoid DB calls

    app.dependency_overrides[get_api] = lambda: api

    # Patch lifespan dependencies (including MemexAPI to avoid real construction,
    # setup_auth/setup_rate_limiting to avoid "Cannot add middleware" errors)
    with (
        patch('memex_core.server.get_metastore', return_value=mock_metastore),
        patch('memex_core.server.get_filestore', return_value=mock_filestore),
        patch('memex_core.server.parse_memex_config', return_value=mock_config),
        patch('memex_core.server.setup_auth'),
        patch('memex_core.server.setup_rate_limiting'),
        patch('memex_core.server.configure_logging'),
        patch('memex_core.server.MemexAPI', return_value=api),
        patch('memex_core.server.get_embedding_model', new_callable=AsyncMock),
        patch('memex_core.server.get_reranking_model', new_callable=AsyncMock),
        patch('memex_core.server.get_ner_model', new_callable=AsyncMock),
        patch('memex_core.server.run_scheduler_with_leader_election', new_callable=AsyncMock),
    ):
        with TestClient(app) as client:
            response = client.get('/api/v1/entities?q=match')
            assert response.status_code == 200
            import json

            data = [json.loads(line) for line in response.text.strip().split('\n') if line]
            assert len(data) == 1
            assert data[0]['name'] == 'Search Match'

    app.dependency_overrides.clear()
