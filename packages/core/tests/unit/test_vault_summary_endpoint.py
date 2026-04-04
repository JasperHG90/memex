"""Tests for /vaults/{vault_id}/summary endpoints."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import VaultSummaryConfig
from memex_core.memory.sql_models import VaultSummary
from memex_core.server import app
from memex_core.server.common import get_api


@pytest.fixture
def vault_id():
    return uuid4()


@pytest.fixture
def mock_summary(vault_id):
    now = datetime.now(timezone.utc)
    return VaultSummary(
        id=uuid4(),
        vault_id=vault_id,
        summary='This vault contains AI research notes.',
        topics=[{'name': 'AI', 'note_count': 5, 'description': 'AI research'}],
        stats={'total_notes': 5},
        version=3,
        notes_incorporated=5,
        patch_log=[],
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mock_api(mock_summary):
    api = AsyncMock()
    api.config = SimpleNamespace(
        server=SimpleNamespace(
            default_active_vault='default-vault',
            vault_summary=VaultSummaryConfig(),
        )
    )
    api.vault_summary = AsyncMock()
    api.vault_summary.get_summary.return_value = mock_summary
    api.vault_summary.regenerate_summary.return_value = mock_summary
    return api


@pytest.fixture
def client(mock_api):
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides = {}


class TestGetVaultSummary:
    def test_returns_summary(self, client, vault_id, mock_api):
        response = client.get(f'/api/v1/vaults/{vault_id}/summary')
        assert response.status_code == 200
        data = response.json()
        assert data['summary'] == 'This vault contains AI research notes.'
        assert data['vault_id'] == str(vault_id)
        assert data['version'] == 3
        assert data['notes_incorporated'] == 5
        assert len(data['topics']) == 1
        assert data['topics'][0]['name'] == 'AI'

    def test_returns_404_when_no_summary(self, client, vault_id, mock_api):
        mock_api.vault_summary.get_summary.return_value = None
        response = client.get(f'/api/v1/vaults/{vault_id}/summary')
        assert response.status_code == 404

    def test_returns_500_on_error(self, client, vault_id, mock_api):
        mock_api.vault_summary.get_summary.side_effect = RuntimeError('DB error')
        response = client.get(f'/api/v1/vaults/{vault_id}/summary')
        assert response.status_code == 500


class TestRegenerateVaultSummary:
    def test_regenerate_returns_summary(self, client, vault_id, mock_api):
        response = client.post(f'/api/v1/vaults/{vault_id}/summary/regenerate')
        assert response.status_code == 200
        data = response.json()
        assert data['summary'] == 'This vault contains AI research notes.'
        mock_api.vault_summary.regenerate_summary.assert_called_once_with(vault_id)

    def test_regenerate_returns_500_on_error(self, client, vault_id, mock_api):
        mock_api.vault_summary.regenerate_summary.side_effect = RuntimeError('LLM failed')
        response = client.post(f'/api/v1/vaults/{vault_id}/summary/regenerate')
        assert response.status_code == 500
