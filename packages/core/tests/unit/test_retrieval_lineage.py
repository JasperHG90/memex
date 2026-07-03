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
    mock_api = AsyncMock()
    mock_api.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='default-vault'))
    mock_api.resolve_vault_identifier.return_value = UUID('00000000-0000-0000-0000-000000000001')
    return mock_api


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


def test_search_memories_flags_degraded_from_contextvar(client, mock_api):
    """The endpoint sets a request-scoped accumulator; when the engine's
    statement_timeout fallback records dropped strategies into it, every returned
    DTO is flagged degraded so the agent knows the results are partial."""
    from memex_core.memory.retrieval.engine import _SEARCH_DEGRADED_DROPPED

    fact_unit = SimpleNamespace(
        id=uuid4(),
        note_id=uuid4(),
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

    async def _search_with_timeout_fallback(*args, **kwargs):
        # Simulate what the engine fallback does mid-search: it updates the
        # endpoint-provided accumulator that lives in the ContextVar.
        acc = _SEARCH_DEGRADED_DROPPED.get()
        assert acc is not None, 'endpoint must set the accumulator before calling search'
        acc.update({'graph', 'keyword'})
        return ([fact_unit], None)

    mock_api.search.side_effect = _search_with_timeout_fallback
    mock_api.resolve_source_notes.return_value = {}

    response = client.post('/api/v1/memories/search', json={'query': 'test', 'limit': 10})
    assert response.status_code == 200

    import json

    data = [json.loads(line) for line in response.text.strip().split('\n') if line]
    assert data[0]['degraded'] is True
    assert sorted(data[0]['dropped_strategies']) == ['graph', 'keyword']

    # The ContextVar is reset after the request — no leak into the next one.
    assert _SEARCH_DEGRADED_DROPPED.get() is None


def test_search_memories_not_degraded_by_default(client, mock_api):
    """A normal search (no timeout) leaves the accumulator empty → DTOs not flagged."""
    fact_unit = SimpleNamespace(
        id=uuid4(),
        note_id=uuid4(),
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

    response = client.post('/api/v1/memories/search', json={'query': 'test', 'limit': 10})
    assert response.status_code == 200

    import json

    data = [json.loads(line) for line in response.text.strip().split('\n') if line]
    assert data[0]['degraded'] is False
    assert data[0]['dropped_strategies'] == []
