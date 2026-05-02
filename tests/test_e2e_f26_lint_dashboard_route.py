"""F26 — End-to-end test for GET /api/v1/diagnostics/lint/{vault_id}.

Pairs with the aggregator integration tests (which cover the SQL pivot
correctness directly) — this layer asserts the route surfaces the
aggregator output 1:1, applies require_read auth, and returns 200 even
for vaults with no findings.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient


def _seed_vault_and_proposals(
    postgres_url: str,
    *,
    pending_structural: int = 0,
    pending_quality: int = 0,
    resolved_quality: int = 0,
) -> UUID:
    """Seed a fresh vault + N proposals via raw SQL. Returns the vault id."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> UUID:
        conn = await asyncpg.connect(dsn)
        try:
            vault_id = uuid4()
            vault_name = f'F26-route-{vault_id.hex[:8]}'
            await conn.execute(
                'INSERT INTO vaults (id, name, created_at) VALUES ($1, $2, NOW())',
                vault_id,
                vault_name,
            )
            for _ in range(pending_structural):
                await conn.execute(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                    'suggested_action, status, source) '
                    "VALUES ($1, 'structural', 'memory_unit', $2, 'rule_a', '{}'::jsonb, "
                    "'fix it', 'pending', 'rule')",
                    vault_id,
                    str(uuid4()),
                )
            for _ in range(pending_quality):
                await conn.execute(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                    'suggested_action, status, source) '
                    "VALUES ($1, 'quality', 'memory_unit', $2, 'rule_b', '{}'::jsonb, "
                    "'fix it', 'pending', 'llm')",
                    vault_id,
                    str(uuid4()),
                )
            for _ in range(resolved_quality):
                await conn.execute(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                    'suggested_action, status, source) '
                    "VALUES ($1, 'quality', 'memory_unit', $2, 'rule_c', '{}'::jsonb, "
                    "'fix it', 'resolved', 'rule')",
                    vault_id,
                    str(uuid4()),
                )
            return vault_id
        finally:
            await conn.close()

    return asyncio.run(_seed())


@pytest.mark.integration
def test_route_returns_200_with_pivot_payload(client: TestClient, postgres_url: str):
    """Happy path: route returns 200 with the documented payload shape."""
    vault_id = _seed_vault_and_proposals(
        postgres_url, pending_structural=2, pending_quality=1, resolved_quality=1
    )
    resp = client.get(f'/api/v1/diagnostics/lint/{vault_id}')
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body['vault_id'] == str(vault_id)
    assert {'counts_by_type_status_source', 'pending_by_type', 'top_5_pending'} <= set(body)
    assert body['pending_by_type'] == {'structural': 2, 'quality': 1}
    counts = {
        (r['lint_type'], r['status'], r['source']): r['count']
        for r in body['counts_by_type_status_source']
    }
    assert counts == {
        ('structural', 'pending', 'rule'): 2,
        ('quality', 'pending', 'llm'): 1,
        ('quality', 'resolved', 'rule'): 1,
    }
    assert len(body['top_5_pending']) == 3


@pytest.mark.integration
def test_route_empty_vault_returns_200_empty_dashboard(client: TestClient, postgres_url: str):
    """A vault with no MaintenanceProposal rows returns 200 with empty pivots —
    the dashboard MUST never 404 on a fresh vault."""
    vault_id = _seed_vault_and_proposals(postgres_url)
    resp = client.get(f'/api/v1/diagnostics/lint/{vault_id}')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['vault_id'] == str(vault_id)
    assert body['counts_by_type_status_source'] == []
    assert body['pending_by_type'] == {}
    assert body['top_5_pending'] == []


@pytest.mark.integration
def test_route_unknown_vault_uuid_returns_200_empty(client: TestClient):
    """Per-vault aggregator filter — a UUID that doesn't exist as a vault still
    yields a clean empty dashboard (the aggregator just sees zero rows). The
    route does NOT pre-validate vault existence; consistent with /diagnostics/summary.
    """
    bogus = uuid4()
    resp = client.get(f'/api/v1/diagnostics/lint/{bogus}')
    assert resp.status_code == 200
    body = resp.json()
    assert body['vault_id'] == str(bogus)
    assert body['pending_by_type'] == {}


@pytest.mark.integration
def test_route_invalid_uuid_returns_422(client: TestClient):
    """Path parameter must be a UUID — non-UUID returns 422."""
    resp = client.get('/api/v1/diagnostics/lint/not-a-uuid')
    assert resp.status_code == 422
