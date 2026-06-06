"""Unit tests for POST /api/v1/memories/by-ids.

The route delegates to ``MemexAPI.get_memory_units_by_ids`` after a
``check_vault_access`` gate. These tests use FastAPI's
dependency_overrides to bypass auth and stub the API surface, verifying:

- 200 + serialized DTOs on the happy path
- ``embedding`` stays null by default even when the ORM row carries a vector
- ``include_vectors=true`` populates ``embedding``
- empty ``unit_ids`` returns an empty list
- the 500-ID request bound is enforced (422)
- vault scoping enforced via check_vault_access (403 for unauthorized vault)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memex_core.server import app
from memex_core.server.auth import (
    AuthContext,
    Permission,
    Policy,
    get_auth_context,
)


def _build_unit(vault_id: str, embedding: list[float] | None = None):
    """ORM-shape namespace that ``build_memory_unit_dto`` can serialize."""
    return SimpleNamespace(
        id=uuid4(),
        note_id=str(uuid4()),
        vault_id=vault_id,
        chunk_id=uuid4(),
        text='Sarah Chen leads Project Alpha.',
        fact_type='world',
        status='active',
        mentioned_at=None,
        event_date=None,
        occurred_start=None,
        occurred_end=None,
        unit_metadata={},
        confidence=1.0,
        embedding=embedding if embedding is not None else [0.25, -0.5, 0.75],
    )


@pytest.fixture
def client_with_stubbed_api():
    """TestClient with stubbed MemexAPI and bypassed auth."""
    vault_id = '22222222-2222-2222-2222-222222222222'
    foreign_vault_id = '33333333-3333-3333-3333-333333333333'

    mock_api = SimpleNamespace(
        get_memory_units_by_ids=AsyncMock(return_value=[_build_unit(vault_id)]),
        resolve_vault_identifier=AsyncMock(side_effect=lambda v: v),
    )
    app.state.api = mock_api
    app.dependency_overrides[get_auth_context] = lambda: None

    yield TestClient(app), mock_api, vault_id, foreign_vault_id

    if hasattr(app.state, 'api'):
        del app.state.api
    app.dependency_overrides.pop(get_auth_context, None)


class TestGetMemoryUnitsByIdsEndpoint:
    def test_returns_200_with_dtos(self, client_with_stubbed_api) -> None:
        client, mock_api, vault_id, _ = client_with_stubbed_api
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [str(uuid4())], 'vault_id': vault_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]['text'] == 'Sarah Chen leads Project Alpha.'

    def test_embedding_null_by_default_despite_loaded_row(self, client_with_stubbed_api) -> None:
        """The ORM stub carries a vector; the default response must NOT."""
        client, _, vault_id, _ = client_with_stubbed_api
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [str(uuid4())], 'vault_id': vault_id},
        )
        assert resp.status_code == 200
        assert resp.json()[0]['embedding'] is None

    def test_include_vectors_false_explicit(self, client_with_stubbed_api) -> None:
        client, _, vault_id, _ = client_with_stubbed_api
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={
                'unit_ids': [str(uuid4())],
                'vault_id': vault_id,
                'include_vectors': False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()[0]['embedding'] is None

    def test_include_vectors_true_populates_embedding(self, client_with_stubbed_api) -> None:
        client, mock_api, vault_id, _ = client_with_stubbed_api
        mock_api.get_memory_units_by_ids = AsyncMock(
            return_value=[_build_unit(vault_id, embedding=[0.1, 0.2, 0.3])]
        )
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={
                'unit_ids': [str(uuid4())],
                'vault_id': vault_id,
                'include_vectors': True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()[0]['embedding'] == pytest.approx([0.1, 0.2, 0.3])

    def test_calls_api_with_correct_args(self, client_with_stubbed_api) -> None:
        client, mock_api, vault_id, _ = client_with_stubbed_api
        unit_id = uuid4()
        client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [str(unit_id)], 'vault_id': vault_id},
        )
        mock_api.get_memory_units_by_ids.assert_called_once()
        args = mock_api.get_memory_units_by_ids.call_args.args
        assert args[0] == [unit_id]
        assert str(args[1]) == vault_id

    def test_empty_unit_ids_returns_empty_list(self, client_with_stubbed_api) -> None:
        client, mock_api, vault_id, _ = client_with_stubbed_api
        mock_api.get_memory_units_by_ids = AsyncMock(return_value=[])
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [], 'vault_id': vault_id},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_more_than_500_ids_rejected(self, client_with_stubbed_api) -> None:
        client, _, vault_id, _ = client_with_stubbed_api
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={
                'unit_ids': [str(uuid4()) for _ in range(501)],
                'vault_id': vault_id,
            },
        )
        assert resp.status_code == 422

    def test_vault_id_required(self, client_with_stubbed_api) -> None:
        client, _, _, _ = client_with_stubbed_api
        resp = client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [str(uuid4())]},
        )
        assert resp.status_code == 422

    def test_vault_isolation_blocks_cross_vault_request(self, client_with_stubbed_api) -> None:
        """If the auth context restricts to a specific vault list, a request
        for a different vault must return 403 from check_vault_access."""
        client, mock_api, vault_id, foreign_vault_id = client_with_stubbed_api

        from uuid import UUID as _UUID

        async def _resolve(v):
            return _UUID(v) if isinstance(v, str) else v

        mock_api.resolve_vault_identifier = AsyncMock(side_effect=_resolve)

        async def _restricted_auth():
            return AuthContext(
                key_prefix='test...',
                key_name='restricted',
                policy=Policy.READER,
                permissions=frozenset({Permission.READ}),
                vault_ids=[vault_id],
                read_vault_ids=None,
            )

        app.dependency_overrides[get_auth_context] = _restricted_auth

        resp = client.post(
            '/api/v1/memories/by-ids',
            json={'unit_ids': [str(uuid4())], 'vault_id': foreign_vault_id},
        )
        assert resp.status_code == 403
