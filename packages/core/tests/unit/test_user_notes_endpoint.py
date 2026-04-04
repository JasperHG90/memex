"""Unit tests for PATCH /api/v1/notes/{note_id}/user-notes (AC-C05)."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock
from fastapi import FastAPI

from memex_core.server.notes import router
from memex_core.server.common import get_api
from memex_core.server.auth import require_write


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.update_user_notes = AsyncMock(
        return_value={'note_id': str(uuid4()), 'units_deleted': 2, 'units_created': 3}
    )
    return api


@pytest.fixture
def app(mock_api):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_api] = lambda: mock_api
    app.dependency_overrides[require_write] = lambda: None
    yield app
    app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_patch_user_notes_endpoint(mock_api, app):
    """AC-C05: PATCH endpoint calls MemexAPI.update_user_notes()."""
    note_id = uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.patch(
            f'/api/v1/notes/{note_id}/user-notes',
            json={'user_notes': 'My new annotation'},
        )

    assert response.status_code == 200
    data = response.json()
    assert 'note_id' in data
    assert data['units_deleted'] == 2
    assert data['units_created'] == 3
    mock_api.update_user_notes.assert_called_once()


@pytest.mark.asyncio
async def test_patch_user_notes_null(mock_api, app):
    """AC-C07: Setting user_notes to null works."""
    note_id = uuid4()
    mock_api.update_user_notes.return_value = {
        'note_id': str(note_id),
        'units_deleted': 5,
        'units_created': 0,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.patch(
            f'/api/v1/notes/{note_id}/user-notes',
            json={'user_notes': None},
        )

    assert response.status_code == 200
    data = response.json()
    assert data['units_created'] == 0
