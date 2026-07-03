"""Unit tests for GET /api/v1/notes/{note_id}/memory_units.

The route delegates to ``MemexAPI.list_memory_units_by_note`` after a
``check_vault_access`` gate. These tests use FastAPI's
dependency_overrides to bypass auth and stub the API surface, verifying:

- 200 + serialized DTOs on the happy path
- ``vault_id`` query param required
- empty result list when the note has no extracted units
- vault scoping enforced via check_vault_access (auth context restricts
  to specific vault_ids)
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


def _build_unit(note_id: str, vault_id: str):
    """Construct a memory unit ORM-shape namespace that build_memory_unit_dto can serialize.

    Mirrors the attributes ``server/common.build_memory_unit_dto`` reads off
    a ``MemoryUnit`` ORM instance (see ``server/common.py:195+``).
    """
    return SimpleNamespace(
        id=uuid4(),
        note_id=note_id,
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
    )


@pytest.fixture
def client_with_stubbed_api() -> TestClient:
    """TestClient with stubbed MemexAPI and bypassed auth."""
    note_id = '11111111-1111-1111-1111-111111111111'
    vault_id = '22222222-2222-2222-2222-222222222222'
    foreign_vault_id = '33333333-3333-3333-3333-333333333333'

    mock_api = SimpleNamespace(
        list_memory_units_by_note=AsyncMock(return_value=[_build_unit(note_id, vault_id)]),
        resolve_vault_identifier=AsyncMock(side_effect=lambda v: v),
    )
    app.state.api = mock_api
    # No auth restriction by default — tests override get_auth_context as needed.
    app.dependency_overrides[get_auth_context] = lambda: None

    yield TestClient(app), mock_api, note_id, vault_id, foreign_vault_id

    if hasattr(app.state, 'api'):
        del app.state.api
    app.dependency_overrides.pop(get_auth_context, None)


class TestListMemoryUnitsByNoteEndpoint:
    def test_returns_200_with_dtos(self, client_with_stubbed_api) -> None:
        client, mock_api, note_id, vault_id, _ = client_with_stubbed_api
        resp = client.get(
            f'/api/v1/notes/{note_id}/memory_units',
            params={'vault_id': vault_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]['note_id'] == note_id

    def test_calls_api_with_correct_vault_scope(self, client_with_stubbed_api) -> None:
        client, mock_api, note_id, vault_id, _ = client_with_stubbed_api
        client.get(
            f'/api/v1/notes/{note_id}/memory_units',
            params={'vault_id': vault_id},
        )
        mock_api.list_memory_units_by_note.assert_called_once()
        args = mock_api.list_memory_units_by_note.call_args.args
        # Passed as positional args (note_id, vault_id) — types are UUID
        assert str(args[0]) == note_id
        assert str(args[1]) == vault_id

    def test_returns_empty_list_when_no_units(self, client_with_stubbed_api) -> None:
        client, mock_api, note_id, vault_id, _ = client_with_stubbed_api
        mock_api.list_memory_units_by_note = AsyncMock(return_value=[])
        resp = client.get(
            f'/api/v1/notes/{note_id}/memory_units',
            params={'vault_id': vault_id},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_vault_id_query_param_required(self, client_with_stubbed_api) -> None:
        client, _, note_id, _, _ = client_with_stubbed_api
        resp = client.get(f'/api/v1/notes/{note_id}/memory_units')
        # FastAPI returns 422 when a required Query param is missing.
        assert resp.status_code == 422

    def test_silently_returns_empty_when_note_belongs_to_a_different_authorized_vault(
        self, client_with_stubbed_api
    ) -> None:
        """If a caller has access to vaults A and B and asks for note_X
        (which lives in A) under vault_id=B, ``check_vault_access`` passes
        (B is allowed) and the SQL query filters on (note_id=X AND
        vault_id=B), returning []. This is data-hiding rather than 403,
        consistent with ``get_memory_units_by_chunks``. Pinning this so a
        future refactor can't silently change the behavior to 404 / leak."""
        client, mock_api, note_id, vault_id, foreign_vault_id = client_with_stubbed_api
        mock_api.list_memory_units_by_note = AsyncMock(return_value=[])
        resp = client.get(
            f'/api/v1/notes/{note_id}/memory_units',
            params={'vault_id': foreign_vault_id},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_vault_isolation_blocks_cross_vault_request(self, client_with_stubbed_api) -> None:
        """If the auth context restricts to a specific vault list, a request
        for a different vault must return 403 from check_vault_access."""
        client, mock_api, note_id, vault_id, foreign_vault_id = client_with_stubbed_api

        # Override get_auth_context to return an auth context that only
        # allows access to `vault_id` (not `foreign_vault_id`).
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

        resp = client.get(
            f'/api/v1/notes/{note_id}/memory_units',
            params={'vault_id': foreign_vault_id},
        )
        assert resp.status_code == 403
