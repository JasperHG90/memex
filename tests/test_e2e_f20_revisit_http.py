"""End-to-end tests for F20 — POST /api/v1/memory/review + GET /api/v1/memory/due_for_review.

Covers the HTTP wire surface added so non-in-process clients (the
Hermes plugin via ``RemoteMemexAPI``) can call the F20 verbs. Mirrors
``test_e2e_f29_outcomes_route.py`` in shape — direct seed via asyncpg,
real FastAPI app via TestClient, real Postgres.

Wave 0 vault-scoping invariant: cross-vault calls must surface as HTTP
403 (PermissionError → 403 mapping in the route). Without this guard a
caller knowing only a UUID could review a unit they don't own; the
service raises PermissionError and the route forwards it.

Quality int|str both accepted; bool rejected at the Pydantic gate.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient


def _seed_unit_eligible(postgres_url: str) -> tuple[UUID, UUID]:
    """Insert a MemoryUnit row that passes the F20 5-gate eligibility predicate.

    intent_class='durable', status='active', is_deprioritized=false,
    confidence=1.0, success_co_count=0, failure_co_count=0
    (cold-start mw_score = (0+1)/(0+0+2) = 0.5, exceeds threshold 0.4).

    Returns (unit_id, vault_id).
    """
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> tuple[UUID, UUID]:
        conn = await asyncpg.connect(dsn)
        try:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = 'global' LIMIT 1")
            assert vault_row is not None, 'global vault missing — fixture broken'
            vault_id = vault_row['id']
            unit_id = uuid4()
            await conn.execute(
                'INSERT INTO memory_units '
                '(id, vault_id, fact_type, text, status, is_deprioritized, intent_class, '
                'risk_class, confidence, event_date, success_co_count, failure_co_count, '
                'revisit_review_count, created_at, updated_at) '
                "VALUES ($1, $2, 'observation', $3, 'active', FALSE, 'durable', 'none', 1.0, "
                'NOW(), 0, 0, 0, NOW(), NOW())',
                unit_id,
                vault_id,
                f'F20 e2e test unit {unit_id}',
            )
            return unit_id, vault_id
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_seed())
    finally:
        loop.close()


def _seed_secondary_vault(postgres_url: str) -> UUID:
    """Insert a second vault so cross-vault rejection can be exercised."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> UUID:
        conn = await asyncpg.connect(dsn)
        try:
            new_id = uuid4()
            await conn.execute(
                "INSERT INTO vaults (id, name, description) VALUES ($1, $2, '')",
                new_id,
                f'f20-other-{new_id.hex[:8]}',
            )
            return new_id
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_seed())
    finally:
        loop.close()


def _read_unit_review_state(
    postgres_url: str, unit_id: UUID
) -> tuple[float | None, float | None, int]:
    """Return (revisit_stability, revisit_difficulty, revisit_review_count)."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _q() -> tuple[float | None, float | None, int]:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                'SELECT revisit_stability, revisit_difficulty, revisit_review_count '
                'FROM memory_units WHERE id = $1',
                unit_id,
            )
            assert row is not None
            return row['revisit_stability'], row['revisit_difficulty'], row['revisit_review_count']
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_q())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# POST /api/v1/memory/review — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_review_good_advances_schedule_and_returns_payload(client: TestClient, postgres_url: str):
    """A first GOOD review writes FSRS-5 init state + advances revisit_due_at."""
    unit_id, vault_id = _seed_unit_eligible(postgres_url)
    before = _read_unit_review_state(postgres_url, unit_id)
    assert before == (None, None, 0), f'expected pristine review state pre-call, got {before!r}'

    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': 'good',
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['unit_id'] == str(unit_id)
    assert body['quality'] == 'good'
    assert body['interval_days'] >= 1
    assert body['review_count'] == 0  # GOOD resets streak (was 0)
    assert body['auto_deprioritized'] is False
    assert 'next_review_at' in body

    after = _read_unit_review_state(postgres_url, unit_id)
    stab, diff, count = after
    assert stab is not None and stab > 0, f'stability not written: {stab!r}'
    assert diff is not None and 1.0 <= diff <= 10.0, f'difficulty out of bounds: {diff!r}'
    assert count == 0


@pytest.mark.integration
def test_review_accepts_int_quality(client: TestClient, postgres_url: str):
    """Quality as FSRS-5 IntEnum value (3 = GOOD) is accepted on the wire."""
    unit_id, vault_id = _seed_unit_eligible(postgres_url)
    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': 3,
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()['quality'] == 'good'


@pytest.mark.integration
def test_review_again_increments_streak_counter(client: TestClient, postgres_url: str):
    """A single AGAIN review bumps revisit_review_count to 1 (no auto-deprio yet)."""
    unit_id, vault_id = _seed_unit_eligible(postgres_url)
    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': 'again',
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['review_count'] == 1
    assert body['auto_deprioritized'] is False


# ---------------------------------------------------------------------------
# POST /api/v1/memory/review — bad inputs / vault scoping
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_review_cross_vault_rejected_with_403(client: TestClient, postgres_url: str):
    """Wave 0 vault-scoping invariant: a unit from vault A cannot be reviewed
    under vault B's id. The service raises PermissionError → HTTP 403.

    Without this, knowing a UUID would let any caller mutate any vault's
    units. The service guard runs after the row-lock and before any FSRS
    advance / counter write / audit emission — so the database state must
    NOT change.
    """
    unit_id, owner_vault = _seed_unit_eligible(postgres_url)
    other_vault = _seed_secondary_vault(postgres_url)
    assert owner_vault != other_vault

    before = _read_unit_review_state(postgres_url, unit_id)

    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': 'good',
            'vault_id': str(other_vault),
        },
    )
    assert resp.status_code == 403, resp.text
    assert 'does not belong to vault' in resp.json()['detail']

    after = _read_unit_review_state(postgres_url, unit_id)
    assert after == before, f'cross-vault attempt mutated state: before={before!r} after={after!r}'


@pytest.mark.integration
def test_review_bool_quality_rejected_at_pydantic_gate(client: TestClient, postgres_url: str):
    """``True`` must NOT silently coerce to ``Quality.AGAIN`` on the wire.

    Without the BeforeValidator guard, Pydantic ``int | str`` would coerce
    True → 1 and the request would record a failure outcome. The route's
    BeforeValidator rejects bool with ValueError → Pydantic surfaces as
    HTTP 422.
    """
    unit_id, vault_id = _seed_unit_eligible(postgres_url)
    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': True,
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()['detail']
    detail_text = str(detail)
    assert 'bool' in detail_text.lower()


@pytest.mark.integration
def test_review_unknown_unit_returns_404(client: TestClient, postgres_url: str):
    """A unit_id that does not exist returns 404, not 500."""
    _, vault_id = _seed_unit_eligible(postgres_url)
    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(uuid4()),
            'quality': 'good',
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.integration
def test_review_missing_vault_id_returns_422(client: TestClient, postgres_url: str):
    """vault_id is REQUIRED on the body — omitting it must be a 422 (Pydantic),
    not a silent default to active vault. Wave 0 invariant.
    """
    unit_id, _ = _seed_unit_eligible(postgres_url)
    resp = client.post(
        '/api/v1/memory/review',
        json={
            'unit_id': str(unit_id),
            'quality': 'good',
        },
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/memory/due_for_review
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_due_for_review_empty_when_no_units_scheduled(client: TestClient, postgres_url: str):
    """A pristine vault with no scheduled units returns []."""
    other_vault = _seed_secondary_vault(postgres_url)
    resp = client.get(
        '/api/v1/memory/due_for_review',
        params={'vault_id': str(other_vault), 'limit': 20},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.integration
def test_due_for_review_returns_unit_after_back_dated_schedule(
    client: TestClient, postgres_url: str
):
    """A unit with revisit_due_at <= now() AND eligible appears in the list."""
    unit_id, vault_id = _seed_unit_eligible(postgres_url)

    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _back_date() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "UPDATE memory_units SET revisit_due_at = NOW() - INTERVAL '1 day', "
                'revisit_stability = 1.0, revisit_difficulty = 5.0 WHERE id = $1',
                unit_id,
            )
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_back_date())
    finally:
        loop.close()

    resp = client.get(
        '/api/v1/memory/due_for_review',
        params={'vault_id': str(vault_id), 'limit': 20},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {row['unit_id'] for row in body}
    assert str(unit_id) in ids, f'expected {unit_id} in due list, got {body!r}'

    row = next(r for r in body if r['unit_id'] == str(unit_id))
    assert 'text_preview' in row
    assert 'revisit_due_at' in row
    assert row['intent_class'] == 'durable'


@pytest.mark.integration
def test_due_for_review_unknown_vault_returns_400(client: TestClient):
    """A nonexistent vault_id returns 400 (caller error)."""
    resp = client.get(
        '/api/v1/memory/due_for_review',
        params={'vault_id': f'nonexistent-{uuid4().hex[:8]}', 'limit': 20},
    )
    assert resp.status_code == 400, resp.text
    assert 'Unknown vault' in resp.json()['detail']
