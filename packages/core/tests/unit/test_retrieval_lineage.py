import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from memex_core.server import app
from memex_core.server.common import get_api
from uuid import UUID, uuid4
from datetime import datetime, timezone
from types import SimpleNamespace
from memex_common.types import FactTypes


@pytest.fixture
def mock_api():
    api_mock = AsyncMock()
    api_mock.config = SimpleNamespace(server=SimpleNamespace(active_vault='default-vault'))
    api_mock.resolve_vault_identifier.return_value = UUID('00000000-0000-0000-0000-000000000001')
    return api_mock


@pytest.fixture
def client(mock_api):
    app.dependency_overrides[get_api] = lambda: mock_api
    return TestClient(app)


def test_retrieve_lineage_resolution(client, mock_api):
    # Setup Data
    doc_1 = uuid4()

    fact_unit_id = uuid4()

    # Fact Unit (Direct Link)
    fact_unit = SimpleNamespace(
        id=fact_unit_id,
        note_id=doc_1,
        text='Fact Text',
        fact_type=FactTypes.WORLD,
        status='active',
        mentioned_at=None,
        occurred_start=None,
        occurred_end=None,
        event_date=datetime.now(timezone.utc),
        vault_id=uuid4(),
        unit_metadata={},
        score=1.0,
    )

    mock_api.search.return_value = ([fact_unit], None)
    mock_api.resolve_source_notes.return_value = {}

    # Execute
    payload = {'query': 'test', 'limit': 10}
    response = client.post('/api/v1/memories/search', json=payload)

    assert response.status_code == 200
    import json

    data = [json.loads(line) for line in response.text.strip().split('\n') if line]

    # Verify Fact Lineage
    assert data[0]['id'] == str(fact_unit_id)
    assert 'source_note_ids' in data[0]
    assert data[0]['source_note_ids'] == [str(doc_1)]
