"""End-to-end tests for F4 — `memex_memory_deprioritize` / `memex_memory_restore`.

Covers:
- T1 — `set_unit_deprioritized` flips column + does NOT cascade (spy + already-stale fixture).
- T2 — Audit log row written with action='memory_deprioritize'; Prometheus counter increments.
- T5 — Restore round-trip via REST + CLI.
- T7 — Idempotency: two deprioritize calls write two AuditLog rows.

Per AC-F4-1..7 + AC-X-3, AC-X-6, AC-X-9.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient


def _query_audit(
    postgres_url: str,
    action: str,
    resource_id: str | None = None,
    *,
    retries: int = 20,
    delay: float = 0.05,
) -> list[dict]:
    """Query audit_logs (mirrors test_e2e_audit_logging.py helper).

    Audit writes are fire-and-forget; retry briefly for the background task.
    """
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _fetch() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            if resource_id:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details FROM audit_logs '
                    'WHERE action = $1 AND resource_id = $2 '
                    'ORDER BY "timestamp" ASC',
                    action,
                    resource_id,
                )
            else:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details FROM audit_logs '
                    'WHERE action = $1 ORDER BY "timestamp" ASC',
                    action,
                )
            out: list[dict] = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get('details'), str):
                    d['details'] = json.loads(d['details'])
                out.append(d)
            return out
        finally:
            await conn.close()

    for _ in range(retries):
        loop = asyncio.new_event_loop()
        try:
            entries = loop.run_until_complete(_fetch())
        finally:
            loop.close()
        if entries:
            return entries
        time.sleep(delay)
    return []


def _seed_unit(postgres_url: str, *, is_deprioritized: bool = False) -> UUID:
    """Insert a MemoryUnit row directly. Avoids ingestion noise so the F4
    flip is the only mutation under test.
    """
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> UUID:
        conn = await asyncpg.connect(dsn)
        try:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = 'global' LIMIT 1")
            assert vault_row is not None, 'global vault missing — fixture broken'
            vault_id = vault_row['id']
            unit_id = uuid4()
            await conn.execute(
                'INSERT INTO memory_units '
                '(id, vault_id, fact_type, text, status, is_deprioritized, intent_class, '
                'risk_class, confidence, event_date, created_at, updated_at) '
                "VALUES ($1, $2, 'observation', $3, 'active', $4, 'durable', 'none', 1.0, "
                'NOW(), NOW(), NOW())',
                unit_id,
                vault_id,
                f'Test unit {unit_id}',
                is_deprioritized,
            )
            return unit_id
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_seed())
    finally:
        loop.close()


def _read_unit(postgres_url: str, unit_id: UUID) -> dict:
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _q() -> dict:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                'SELECT id, is_deprioritized, status FROM memory_units WHERE id = $1',
                unit_id,
            )
            assert row is not None
            return dict(row)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_q())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# T1 — flip + no cascade
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_flip_sets_column_and_no_cascade(client: TestClient, postgres_url: str):
    """T1: deprioritize flips the column AND does NOT touch other units' status."""
    target = _seed_unit(postgres_url, is_deprioritized=False)
    # Seed a sibling 'stale' unit. Per AC-F4-3 the F4 flow must not cascade,
    # so this stale unit's status must remain 'stale' after the flip.
    sibling = _seed_unit(postgres_url, is_deprioritized=False)
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _set_sibling_stale() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("UPDATE memory_units SET status = 'stale' WHERE id = $1", sibling)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_set_sibling_stale())
    finally:
        loop.close()

    resp = client.post(
        f'/api/v1/memories/{target}/deprioritize', json={'reason': 'test cascade guard'}
    )
    assert resp.status_code == 200, resp.text

    target_row = _read_unit(postgres_url, target)
    sibling_row = _read_unit(postgres_url, sibling)
    assert target_row['is_deprioritized'] is True
    assert sibling_row['is_deprioritized'] is False, 'F4 flipped a sibling unit (cascade leaked).'
    assert sibling_row['status'] == 'stale', (
        'F4 must NOT mutate sibling.status (no prune_stale_evidence call).'
    )


# ---------------------------------------------------------------------------
# T2 — audit log + Prometheus counter
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reason_logged_to_audit_logs(client: TestClient, postgres_url: str):
    """T2 part 1: AuditLog row exists with action='memory_deprioritize' + reason."""
    target = _seed_unit(postgres_url)
    reason = f'unit was wrong about {uuid4().hex[:8]}'

    resp = client.post(f'/api/v1/memories/{target}/deprioritize', json={'reason': reason})
    assert resp.status_code == 200

    rows = _query_audit(postgres_url, 'memory_deprioritize', str(target))
    assert len(rows) == 1, f'expected 1 row, got {len(rows)}: {rows}'
    row = rows[0]
    assert row['resource_type'] == 'memory_unit'
    assert row['resource_id'] == str(target)
    assert row['details'] is not None
    assert row['details']['reason'] == reason


@pytest.mark.integration
def test_audit_log_total_counter_increments(client: TestClient, postgres_url: str):
    """T2 part 2: memex_audit_log_total{action="memory_deprioritize"} increments."""
    from memex_core.metrics import MEMEX_AUDIT_LOG_TOTAL

    before = MEMEX_AUDIT_LOG_TOTAL.labels(action='memory_deprioritize')._value.get()

    target = _seed_unit(postgres_url)
    resp = client.post(f'/api/v1/memories/{target}/deprioritize', json={'reason': 'metrics check'})
    assert resp.status_code == 200
    # Block until audit row lands so we know _persist completed.
    rows = _query_audit(postgres_url, 'memory_deprioritize', str(target))
    assert len(rows) == 1

    after = MEMEX_AUDIT_LOG_TOTAL.labels(action='memory_deprioritize')._value.get()
    assert after == before + 1


# ---------------------------------------------------------------------------
# T5 — restore round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_restore_round_trip(client: TestClient, postgres_url: str):
    """T5: deprioritize → restore writes 1 + 1 audit rows; column flips both ways."""
    target = _seed_unit(postgres_url)

    r1 = client.post(f'/api/v1/memories/{target}/deprioritize', json={'reason': 'first call'})
    assert r1.status_code == 200
    assert _read_unit(postgres_url, target)['is_deprioritized'] is True

    r2 = client.post(f'/api/v1/memories/{target}/restore')
    assert r2.status_code == 200
    assert _read_unit(postgres_url, target)['is_deprioritized'] is False

    deprio_rows = _query_audit(postgres_url, 'memory_deprioritize', str(target))
    restore_rows = _query_audit(postgres_url, 'memory_restore', str(target))
    assert len(deprio_rows) == 1
    assert len(restore_rows) == 1


# ---------------------------------------------------------------------------
# T7 — idempotency: two calls write two AuditLog rows
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_double_deprioritize_writes_two_audit_log_rows(client: TestClient, postgres_url: str):
    """T7: behavior (b) — every call writes an AuditLog row.

    Audit logs are append-only by design; calling deprioritize twice with
    different reasons preserves both reasons. The column itself is idempotent
    (False→True→True is fine).
    """
    target = _seed_unit(postgres_url)

    r1 = client.post(f'/api/v1/memories/{target}/deprioritize', json={'reason': 'first'})
    assert r1.status_code == 200
    r2 = client.post(f'/api/v1/memories/{target}/deprioritize', json={'reason': 'second'})
    assert r2.status_code == 200

    rows = _query_audit(postgres_url, 'memory_deprioritize', str(target))
    assert len(rows) == 2
    reasons = {r['details']['reason'] for r in rows}
    assert reasons == {'first', 'second'}


# ---------------------------------------------------------------------------
# 404 — unit not found
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_deprioritize_missing_unit_returns_404(client: TestClient):
    """Bad-path: deprioritize on a unit that doesn't exist returns 404."""
    bogus = uuid4()
    resp = client.post(f'/api/v1/memories/{bogus}/deprioritize', json={'reason': 'never matters'})
    assert resp.status_code == 404
