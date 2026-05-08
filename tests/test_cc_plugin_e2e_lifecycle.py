"""End-to-end lifecycle test for the Claude Code plugin.

Reuses the project-wide ``client`` fixture (real Postgres testcontainer +
FastAPI app + Alembic migrations) to verify the plugin-specific data
contracts that aren't covered elsewhere:

  - KV namespace migration: legacy ``project:<id>:vault`` and new
    ``app:claude-code:project:<id>:vault`` keys both round-trip correctly.
  - The CC and Hermes namespaces don't collide.

The "single-note across PreCompact + SessionEnd appends" semantics are
already pinned by ``tests/test_e2e_append_endpoint.py`` for the underlying
append API. The "auto-tag injection" behavior is pinned by the plugin
unit tests against the actual jq pipeline. The "CLI compatibility"
between scripts and the Memex CLI is pinned by
``packages/claude-code-plugin/tests/integration/test_cli_compatibility.py``.

Together those four layers cover the chain end-to-end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.integration


def _kv_lookup_value(payload: object, key: str) -> str | None:
    """The /kv GET endpoint returns a list of entries; pull the value for ``key``."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get('key') == key:
                return item.get('value')
        return None
    if isinstance(payload, dict):
        return payload.get('value')
    return None


@pytest.mark.parametrize(
    'project_id,legacy_vault,new_vault',
    [
        ('github.com/acme/myapp', 'legacy-vault', 'new-vault'),
        ('plain/path/project', 'legacy-x', 'new-x'),
    ],
)
def test_kv_namespace_round_trip(
    client: TestClient, project_id: str, legacy_vault: str, new_vault: str
) -> None:
    """Both the legacy and new namespaces persist independently and read back exactly."""
    legacy_key = f'project:{project_id}:vault'
    new_key = f'app:claude-code:project:{project_id}:vault'

    r = client.put('/api/v1/kv', json={'key': legacy_key, 'value': legacy_vault})
    assert r.status_code in (200, 201), r.text

    r = client.put('/api/v1/kv', json={'key': new_key, 'value': legacy_vault})
    assert r.status_code in (200, 201), r.text

    r = client.get('/api/v1/kv', params={'key': legacy_key})
    assert r.status_code == 200, r.text
    assert _kv_lookup_value(r.json(), legacy_key) == legacy_vault

    r = client.get('/api/v1/kv', params={'key': new_key})
    assert r.status_code == 200, r.text
    assert _kv_lookup_value(r.json(), new_key) == legacy_vault

    # Now rebind the new key — legacy should be untouched.
    r = client.put('/api/v1/kv', json={'key': new_key, 'value': new_vault})
    assert r.status_code in (200, 201), r.text

    r = client.get('/api/v1/kv', params={'key': legacy_key})
    assert _kv_lookup_value(r.json(), legacy_key) == legacy_vault

    r = client.get('/api/v1/kv', params={'key': new_key})
    assert _kv_lookup_value(r.json(), new_key) == new_vault


def test_kv_app_namespace_does_not_collide_with_hermes(client: TestClient) -> None:
    """``app:claude-code:`` and ``app:hermes:`` keys share the KV table but
    don't collide. This is the whole point of namespacing."""
    cc_key = 'app:claude-code:project:my-repo:vault'
    hermes_key = 'app:hermes:project:my-repo:vault'

    r = client.put('/api/v1/kv', json={'key': cc_key, 'value': 'cc-vault'})
    assert r.status_code in (200, 201), r.text

    r = client.put('/api/v1/kv', json={'key': hermes_key, 'value': 'hermes-vault'})
    assert r.status_code in (200, 201), r.text

    r = client.get('/api/v1/kv', params={'key': cc_key})
    assert _kv_lookup_value(r.json(), cc_key) == 'cc-vault'

    r = client.get('/api/v1/kv', params={'key': hermes_key})
    assert _kv_lookup_value(r.json(), hermes_key) == 'hermes-vault'
