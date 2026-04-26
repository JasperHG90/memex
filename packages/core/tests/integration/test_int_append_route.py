"""HTTP contract tests for ``POST /api/v1/notes/append``.

Drives the route via ``httpx.AsyncClient + ASGITransport`` (no lifespan, no
real DB) with the ``MemexAPI`` dependency overridden to a mock. Validates:

* request schema (Pydantic 422 for bad shapes)
* happy-path response shape (200 / NoteAppendResponse)
* error mapping (404, 409, 503) per ``server/common.py:_handle_error``
* OpenAPI advertises the route
"""

from __future__ import annotations

from typing import AsyncGenerator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from memex_common.exceptions import (
    AppendIdConflictError,
    AppendLockTimeoutError,
    FeatureDisabledError,
    NoteNotAppendableError,
    ResourceNotFoundError,
)
from memex_core.api import MemexAPI
from memex_core.server import app
from memex_core.server.common import get_api


@pytest.fixture
def mock_api():
    """Async-mocked MemexAPI plugged into ``app.dependency_overrides``.

    ``api.notes.resolve_note_id`` is wired up so the route's pre-resolve
    step (used for vault-access enforcement) sees a sensible default. Tests
    that need a specific resolved vault override it.
    """
    api = AsyncMock(spec=MemexAPI)
    api.notes = AsyncMock()
    # Default: identifier → (note_id, vault_id) where vault_id mirrors the
    # one in the request when given. Tests that exercise other paths can
    # override.
    api.notes.resolve_note_id = AsyncMock(
        side_effect=lambda note_id, note_key, vault_id: (
            note_id or uuid4(),
            vault_id if isinstance(vault_id, UUID) else (UUID(vault_id) if vault_id else uuid4()),
        )
    )
    app.dependency_overrides[get_api] = lambda: api
    yield api
    app.dependency_overrides.pop(get_api, None)


@pytest_asyncio.fixture
async def http(mock_api) -> AsyncGenerator[AsyncClient, None]:
    """AsyncClient backed by an ASGI transport — no lifespan, no DB."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        yield c


def _ok_payload(note_id: UUID, append_id: UUID, *, status: str = 'success') -> dict:
    return {
        'status': status,
        'note_id': str(note_id),
        'append_id': str(append_id),
        'content_hash': 'abc123',
        'delta_bytes': 5,
        'new_unit_ids': [],
    }


@pytest.mark.integration
class TestAppendRouteHappyPath:
    """Successful append round-trips the response shape."""

    @pytest.mark.asyncio
    async def test_append_by_note_id_returns_200(self, http: AsyncClient, mock_api):
        note_id = uuid4()
        append_id = uuid4()
        mock_api.append_to_note.return_value = _ok_payload(note_id, append_id)

        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(note_id),
                'delta': 'hello',
                'append_id': str(append_id),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['status'] == 'success'
        assert body['note_id'] == str(note_id)
        assert body['append_id'] == str(append_id)
        assert body['delta_bytes'] == 5
        assert body['new_unit_ids'] == []

    @pytest.mark.asyncio
    async def test_append_by_note_key_with_vault(self, http: AsyncClient, mock_api):
        note_id = uuid4()
        append_id = uuid4()
        vault_id = uuid4()
        mock_api.append_to_note.return_value = _ok_payload(note_id, append_id)

        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_key': 'session-key',
                'vault_id': str(vault_id),
                'delta': 'hi',
                'append_id': str(append_id),
            },
        )
        assert resp.status_code == 200, resp.text
        call_kwargs = mock_api.append_to_note.call_args.kwargs
        assert call_kwargs['note_key'] == 'session-key'
        assert UUID(str(call_kwargs['vault_id'])) == vault_id

    @pytest.mark.asyncio
    async def test_append_replayed_status_passes_through(self, http: AsyncClient, mock_api):
        note_id = uuid4()
        append_id = uuid4()
        mock_api.append_to_note.return_value = _ok_payload(note_id, append_id, status='replayed')

        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(note_id),
                'delta': 'x',
                'append_id': str(append_id),
            },
        )
        assert resp.status_code == 200
        assert resp.json()['status'] == 'replayed'


@pytest.mark.integration
class TestAppendRouteValidation:
    """Pydantic-level validation runs BEFORE the route handler is invoked."""

    @pytest.mark.asyncio
    async def test_missing_identifier_422(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={'delta': 'x', 'append_id': str(uuid4())},
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_note_key_without_vault_422(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_key': 'some-key',
                'delta': 'x',
                'append_id': str(uuid4()),
            },
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_delta_422(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(uuid4()),
                'delta': '',
                'append_id': str(uuid4()),
            },
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_delta_422(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(uuid4()),
                'delta': 'x' * 200_001,
                'append_id': str(uuid4()),
            },
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_frontmatter_delta_rejected(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(uuid4()),
                'delta': '---\nkey: val\n---\nbody',
                'append_id': str(uuid4()),
            },
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_joiner_rejected(self, http: AsyncClient, mock_api):
        resp = await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(uuid4()),
                'delta': 'x',
                'append_id': str(uuid4()),
                'joiner': 'crlf',
            },
        )
        assert resp.status_code == 422
        mock_api.append_to_note.assert_not_called()


@pytest.mark.integration
class TestAppendRouteErrorMapping:
    """Service-layer errors map to the documented HTTP status codes."""

    @staticmethod
    async def _post(http: AsyncClient):
        return await http.post(
            '/api/v1/notes/append',
            json={
                'note_id': str(uuid4()),
                'delta': 'x',
                'append_id': str(uuid4()),
            },
        )

    @pytest.mark.asyncio
    async def test_missing_parent_returns_404(self, http: AsyncClient, mock_api):
        mock_api.append_to_note.side_effect = ResourceNotFoundError('note not found')
        resp = await self._post(http)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_append_id_conflict_returns_409(self, http: AsyncClient, mock_api):
        mock_api.append_to_note.side_effect = AppendIdConflictError(
            'append_id reused with different parent/delta'
        )
        resp = await self._post(http)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_archived_parent_returns_409(self, http: AsyncClient, mock_api):
        mock_api.append_to_note.side_effect = NoteNotAppendableError('note is archived')
        resp = await self._post(http)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lock_timeout_returns_503_with_retry_after(self, http: AsyncClient, mock_api):
        mock_api.append_to_note.side_effect = AppendLockTimeoutError('lock acquire timed out')
        resp = await self._post(http)
        assert resp.status_code == 503
        assert resp.headers.get('retry-after') == '5'

    @pytest.mark.asyncio
    async def test_kill_switch_returns_503(self, http: AsyncClient, mock_api):
        mock_api.append_to_note.side_effect = FeatureDisabledError(
            'append endpoint disabled by config'
        )
        resp = await self._post(http)
        assert resp.status_code == 503
        assert resp.headers.get('retry-after') == '5'


@pytest.mark.integration
class TestAppendRouteOpenAPI:
    """The route is advertised in the OpenAPI schema so SDKs can discover it."""

    @pytest.mark.asyncio
    async def test_openapi_includes_append_route(self, http: AsyncClient, mock_api):
        resp = await http.get('/openapi.json')
        assert resp.status_code == 200
        spec = resp.json()
        assert '/api/v1/notes/append' in spec['paths']
        post = spec['paths']['/api/v1/notes/append']['post']
        # Document each error code we promise so SDKs and clients can codegen.
        for code in ('200', '404', '409', '503'):
            assert code in post['responses'], f'Missing {code} in OpenAPI spec'


@pytest.mark.integration
class TestAppendRouteVaultAccess:
    """Vault-restricted callers cannot append to notes outside their allowed set."""

    @pytest.mark.asyncio
    async def test_caller_with_other_vault_only_gets_403(self, http: AsyncClient, mock_api):
        """A caller whose AuthContext only allows vault A is rejected when the
        target note resolves to vault B — even when only ``note_id`` is given."""
        from memex_core.server.auth import AuthContext, Permission, get_auth_context

        target_note = uuid4()
        target_vault = uuid4()
        allowed_vault = uuid4()

        # Resolve returns the parent's actual vault — not the caller's allowed set.
        mock_api.notes.resolve_note_id = AsyncMock(return_value=(target_note, target_vault))
        # check_vault_access calls api.resolve_vault_identifier on each allowed
        # vault and on the requested vault — wire those up.
        mock_api.resolve_vault_identifier = AsyncMock(
            side_effect=lambda v: v if isinstance(v, UUID) else UUID(str(v))
        )

        auth = AuthContext(
            key_prefix='test',
            key_name='restricted',
            policy='reader',
            permissions=frozenset({Permission.WRITE}),
            vault_ids=[str(allowed_vault)],
            read_vault_ids=None,
        )
        app.dependency_overrides[get_auth_context] = lambda: auth
        try:
            resp = await http.post(
                '/api/v1/notes/append',
                json={
                    'note_id': str(target_note),
                    'delta': 'hi',
                    'append_id': str(uuid4()),
                },
            )
        finally:
            app.dependency_overrides.pop(get_auth_context, None)

        assert resp.status_code == 403, resp.text
        # The append_to_note service must NOT have been called.
        mock_api.append_to_note.assert_not_called()
