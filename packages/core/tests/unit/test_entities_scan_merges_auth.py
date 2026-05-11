"""HTTP-level auth gating for POST /api/v1/entities/scan-merges.

The scan endpoint performs cross-vault writes (INSERT/UPDATE on
``maintenance_proposals`` and ``UPDATE entities SET last_merge_scan_at``),
so it MUST require the WRITE permission even though the dispatched
function returns a summary dict. A read-only key — even an
otherwise-valid one — must be rejected before the service is called.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import Policy, POLICY_PERMISSIONS
from memex_core.server import app
from memex_core.server.auth import AuthContext, get_auth_context
from memex_core.server.common import get_api


ALLOWED_VAULT = uuid4()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='vault-a'))
    api.metastore = SimpleNamespace()
    return api


def _make_client(mock_api, auth: AuthContext | None) -> TestClient:
    app.dependency_overrides[get_api] = lambda: mock_api
    app.dependency_overrides[get_auth_context] = lambda: auth
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides = {}


def _reader_key() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='scoped-reader',
        policy=Policy.READER,
        permissions=POLICY_PERMISSIONS[Policy.READER],
        vault_ids=[str(ALLOWED_VAULT)],
        read_vault_ids=None,
    )


def _writer_key() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='writer',
        policy=Policy.WRITER,
        permissions=POLICY_PERMISSIONS[Policy.WRITER],
        vault_ids=None,
        read_vault_ids=None,
    )


class TestScanMergesAuth:
    def test_scan_merges_rejects_read_only_key(self, mock_api, monkeypatch):
        """A reader-policy key must be rejected with 403 before the
        cross-vault write path runs."""
        called = {'invoked': False}

        async def _fake_scan(*_args, **_kwargs):
            called['invoked'] = True
            return {
                'clusters_emitted': 0,
                'clusters_rejected_cohesion': 0,
                'rescan_updated': 0,
                'scanned': 0,
            }

        monkeypatch.setattr(
            'memex_core.services.entity_maintenance.scan_collapse_clusters',
            _fake_scan,
        )
        client = _make_client(mock_api, _reader_key())
        resp = client.post('/api/v1/entities/scan-merges')
        assert resp.status_code == 403, resp.text
        assert called['invoked'] is False

    def test_scan_merges_accepts_write_key(self, mock_api, monkeypatch):
        """A writer-policy key passes the permission gate and the
        underlying scan runs."""
        called = {'invoked': False}

        async def _fake_scan(*_args, **_kwargs):
            called['invoked'] = True
            return {
                'clusters_emitted': 0,
                'clusters_rejected_cohesion': 0,
                'rescan_updated': 0,
                'scanned': 0,
            }

        monkeypatch.setattr(
            'memex_core.services.entity_maintenance.scan_collapse_clusters',
            _fake_scan,
        )
        client = _make_client(mock_api, _writer_key())
        resp = client.post('/api/v1/entities/scan-merges')
        assert resp.status_code == 200, resp.text
        assert called['invoked'] is True
