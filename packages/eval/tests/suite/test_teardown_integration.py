"""Integration tests for ``_RecordOutcome`` teardown — proves that the
DB-direct UPDATE actually reverses MW counters in real Postgres.

Boots a testcontainer Postgres, plants a vault + note + memory_units,
seeds counter values that mimic what ``record_outcome`` would write,
inserts the audit_log rows ``record_outcome`` would create, then calls
the teardown handler with a setup-context snapshot built the same way
the runner builds it.

After teardown:

- ``memory_units.success_co_count`` / ``failure_co_count`` MUST be at
  the captured pre-state for every targeted unit.
- ``unit_entities`` MW counters MUST be decremented by the stamped
  amount.
- ``mental_models`` MW counters MUST be decremented by the stamped
  amount.
- ``audit_logs`` rows the run() created MUST be deleted.

Marker ``integration`` keeps these out of the default test run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Generator
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer


pytestmark = [
    pytest.mark.integration,
    # Module-scoped Postgres fixture requires the event loop to span the
    # module. ``asyncio_mode='auto'`` defaults to function-scoped loops,
    # which would raise ScopeMismatchError when the module-scoped fixture
    # tries to use it (review round-1 HIGH #3).
    pytest.mark.asyncio(loop_scope='module'),
]


# --- Fixtures ---------------------------------------------------------------
#
# Schema: we load the production tables via ``SQLModel.metadata.create_all``
# (review round-1 MEDIUM #7). This guarantees the teardown SQL is exercised
# against the same column types, FKs, and constraints memex_core uses in
# production — not a hand-rolled minimal schema that could drift.


@pytest_asyncio.fixture(loop_scope='module', scope='module')
async def _module_loop_anchor() -> AsyncGenerator[None, None]:
    """No-op module-scoped async fixture so pytest-asyncio binds the
    module-scoped loop early. The test functions use the same loop."""
    yield


@pytest.fixture(scope='module')
def postgres_dsn() -> Generator[str, None, None]:
    """Boot a fresh Postgres testcontainer for the integration tests in
    this module. Pinned to the same image used by core's integration tests
    (``pgvector/pgvector:pg18-trixie``) so we exercise the same backend
    eval will run against in production."""
    container = PostgresContainer('pgvector/pgvector:pg18-trixie')
    container.start()
    try:
        # ``get_connection_url`` returns ``postgresql+psycopg2://...`` — strip
        # the SQLAlchemy dialect, asyncpg only takes the bare scheme.
        url = (
            container.get_connection_url()
            .replace('+psycopg2', '')
            .replace('postgresql+psycopg2', 'postgresql')
        )
        # Some versions return ``postgresql://`` already; others use ``+psycopg2``.
        if '+' in url.split('://', 1)[0]:
            url = 'postgresql://' + url.split('://', 1)[1]
        yield url
    finally:
        container.stop()


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def initialized_db(postgres_dsn: str) -> AsyncGenerator[str, None]:
    """Create the production schema (SQLModel.metadata.create_all) so the
    teardown SQL runs against the same column types / constraints / FKs
    memex_core uses in production. Pgvector + pg_trgm extensions enabled
    to match the engine fixture in core's integration conftest.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    # Force the SQLModel table classes to register before create_all.
    import memex_core.memory.sql_models  # noqa: F401

    engine = create_async_engine(
        postgres_dsn.replace('postgresql://', 'postgresql+asyncpg://'),
        future=True,
        echo=False,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()

    yield postgres_dsn


@pytest_asyncio.fixture
async def clean_db(initialized_db: str) -> AsyncGenerator[str, None]:
    """Truncate every test table before each test."""
    conn = await asyncpg.connect(initialized_db)
    try:
        await conn.execute(
            'TRUNCATE TABLE vaults, notes, memory_units, entities, '
            'unit_entities, mental_models, audit_logs RESTART IDENTITY CASCADE'
        )
    finally:
        await conn.close()
    yield initialized_db


@pytest.fixture
def env_dsn(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point ``MEMEX_EVAL_DATABASE_URL`` at the testcontainer so the
    eval-suite's ``eval_db_session`` connects to it."""
    monkeypatch.setenv('MEMEX_EVAL_DATABASE_URL', clean_db)
    return clean_db


# --- Helpers ----------------------------------------------------------------


async def _seed_unit(
    dsn: str,
    *,
    vault_id: UUID,
    note_id: UUID,
    unit_id: UUID,
    entity_id: UUID,
    success_co: int,
    failure_co: int,
    last_outcome_at: datetime | None,
) -> None:
    """Insert a vault + note + memory_unit + (unit_entity, mental_model)
    pair with the given counter values. Mimics what would exist after a
    suite ingest + N record_outcome calls."""
    # Production schema has NOT NULL on memory_units.text + vault FK.
    # Seed enough columns to satisfy them; leave optional columns at default.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            'INSERT INTO vaults (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING',
            vault_id,
            f'test-vault-{vault_id.hex[:8]}',
        )
        await conn.execute(
            "INSERT INTO notes (id, vault_id, title) VALUES ($1, $2, 'test-note') "
            'ON CONFLICT (id) DO NOTHING',
            note_id,
            vault_id,
        )
        await conn.execute(
            'INSERT INTO memory_units '
            '(id, note_id, vault_id, text, fact_type, event_date, '
            ' success_co_count, failure_co_count, last_outcome_at) '
            "VALUES ($1, $2, $3, 'extracted fact', 'world', now(), $4, $5, $6)",
            unit_id,
            note_id,
            vault_id,
            success_co,
            failure_co,
            last_outcome_at,
        )
        await conn.execute(
            'INSERT INTO entities (id, canonical_name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING',
            entity_id,
            f'test-entity-{entity_id.hex[:8]}',
        )
        await conn.execute(
            'INSERT INTO unit_entities '
            '(unit_id, entity_id, vault_id, success_co_count, failure_co_count) '
            'VALUES ($1, $2, $3, $4, $5) '
            'ON CONFLICT (unit_id, entity_id) DO UPDATE SET '
            'success_co_count = EXCLUDED.success_co_count, '
            'failure_co_count = EXCLUDED.failure_co_count',
            unit_id,
            entity_id,
            vault_id,
            success_co,
            failure_co,
        )
        await conn.execute(
            'INSERT INTO mental_models '
            '(id, entity_id, vault_id, name, version, success_co_count, failure_co_count) '
            'VALUES (gen_random_uuid(), $1, $2, $3, 1, $4, $5)',
            entity_id,
            vault_id,
            f'test-entity-{entity_id.hex[:8]}',
            success_co,
            failure_co,
        )
    finally:
        await conn.close()


async def _seed_audit_log(dsn: str, *, unit_id: UUID, vault_id: UUID, outcome: str, n: int) -> None:
    """Insert N audit_log rows mirroring what ``record_outcome`` writes.

    Each call to ``record_outcome`` adds one ``action='outcome.record'``
    row per unit per call. Stamping count=N with vector len=K yields N*K
    rows in real life; this helper is direct so we can pin the count."""
    conn = await asyncpg.connect(dsn)
    try:
        for _ in range(n):
            await conn.execute(
                'INSERT INTO audit_logs (id, action, resource_type, resource_id, details, timestamp) '
                "VALUES (gen_random_uuid(), 'outcome.record', 'memory_unit', $1, $2::jsonb, now())",
                str(unit_id),
                f'{{"vault_id": "{vault_id}", "outcome": "{outcome}"}}',
            )
    finally:
        await conn.close()


async def _read_unit(dsn: str, unit_id: UUID) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            'SELECT success_co_count, failure_co_count, last_outcome_at '
            'FROM memory_units WHERE id = $1',
            unit_id,
        )
        return dict(row) if row else {}
    finally:
        await conn.close()


async def _read_unit_entity(dsn: str, unit_id: UUID) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            'SELECT success_co_count, failure_co_count FROM unit_entities WHERE unit_id = $1',
            unit_id,
        )
        return dict(row) if row else {}
    finally:
        await conn.close()


async def _read_mental_model(dsn: str, entity_id: UUID, vault_id: UUID) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            'SELECT success_co_count, failure_co_count '
            'FROM mental_models WHERE entity_id = $1 AND vault_id = $2',
            entity_id,
            vault_id,
        )
        return dict(row) if row else {}
    finally:
        await conn.close()


async def _pg_now(dsn: str) -> datetime:
    """Return the connected Postgres' transaction_timestamp.

    Tests use this — NOT ``datetime.now(timezone.utc)`` — to capture
    ``audit_ts_low`` so the eventual `WHERE timestamp >= $audit_ts_low`
    DELETE compares against the same clock that produced the rows
    (review round-2 MEDIUM #1). Python wall-clock can lead PG's clock
    in containerized CI; the resulting timestamp mismatch can leave
    seed audit rows out of the delete range and flake the assertions.
    """
    conn = await asyncpg.connect(dsn)
    try:
        ts = await conn.fetchval('SELECT now()')
        assert isinstance(ts, datetime)
        return ts
    finally:
        await conn.close()


async def _audit_count(
    dsn: str, unit_id: UUID, since: datetime, vault_id: UUID | None = None
) -> int:
    """Count audit rows for ``unit_id`` (and optionally a specific vault)
    that are >= ``since``. ``vault_id`` filters via the JSONB
    ``details->>'vault_id'`` predicate — same shape the production audit
    DELETE uses (review round-2 MEDIUM #5)."""
    conn = await asyncpg.connect(dsn)
    try:
        if vault_id is None:
            n = await conn.fetchval(
                'SELECT COUNT(*) FROM audit_logs '
                "WHERE action = 'outcome.record' "
                'AND resource_id = $1 AND timestamp >= $2',
                str(unit_id),
                since,
            )
        else:
            n = await conn.fetchval(
                'SELECT COUNT(*) FROM audit_logs '
                "WHERE action = 'outcome.record' "
                'AND resource_id = $1 AND timestamp >= $2 '
                "AND details->>'vault_id' = $3",
                str(unit_id),
                since,
                str(vault_id),
            )
        return int(n)
    finally:
        await conn.close()


# --- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_outcome_teardown_reverts_counters_with_prev_state(
    env_dsn: str,
) -> None:
    """The full success path: teardown UPDATEs memory_units back to the
    captured ``prev_state``, decrements unit_entities + mental_models by
    the stamped amount, and DELETEs the audit_logs rows it created."""
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    note_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    pre_last_outcome = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Pretend a prior run stamped success_co=3 (e.g. via record_outcome
    # called 3 times). Pre-state on memory_units is what existed BEFORE
    # the stamp — that's the snapshot the teardown must restore.
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=note_id,
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )

    # The audit log entries the run() created. ``audit_ts_low`` is the
    # earliest of them — teardown deletes ``timestamp >= audit_ts_low``.
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_id, vault_id=vault_id, outcome='success', n=3)
    assert await _audit_count(env_dsn, unit_id, audit_ts_low) == 3

    # Build the per-action context the runner would have stored: prev_state
    # snapshots the unit's pre-stamp counters + last_outcome_at.
    prev_state = {
        str(unit_id): {
            'success_co_count': 0,
            'failure_co_count': 0,
            'last_outcome_at': pre_last_outcome,
        }
    }
    setup_context = {
        'unit_ids': [str(unit_id)],
        'stamped_success': 3,
        'stamped_failure': 0,
        'audit_ts_low': audit_ts_low.isoformat(),
        'prev_state': prev_state,
        'dsn_validated': True,
    }

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={'note_key': 'k', 'success': True, 'count': 3},
        setup_context=setup_context,
    )

    # 1. memory_units restored to pre_last_outcome counters.
    unit = await _read_unit(env_dsn, unit_id)
    assert unit['success_co_count'] == 0
    assert unit['failure_co_count'] == 0
    assert unit['last_outcome_at'] == pre_last_outcome

    # 2. unit_entities decremented (was 3, stamped 3 → 0).
    ue = await _read_unit_entity(env_dsn, unit_id)
    assert ue['success_co_count'] == 0
    assert ue['failure_co_count'] == 0

    # 3. mental_models decremented (was 3, stamped 3 → 0).
    mm = await _read_mental_model(env_dsn, entity_id, vault_id)
    assert mm['success_co_count'] == 0
    assert mm['failure_co_count'] == 0

    # 4. audit_logs cleaned up — no outcome.record rows for this unit
    #    on/after audit_ts_low.
    assert await _audit_count(env_dsn, unit_id, audit_ts_low) == 0

    # 5. The DB-direct path succeeded → fallback flip-cancel was NOT used.
    api.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_record_outcome_teardown_reverts_failure_stamp(env_dsn: str) -> None:
    """Mirror case: a failure-stamp scenario. After teardown,
    failure_co_count is back to the captured pre-state."""
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()

    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=0,
        failure_co=3,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_id, vault_id=vault_id, outcome='failure', n=3)

    prev_state = {
        str(unit_id): {
            'success_co_count': 0,
            'failure_co_count': 0,
            'last_outcome_at': None,
        }
    }
    setup_context = {
        'unit_ids': [str(unit_id)],
        'stamped_success': 0,
        'stamped_failure': 3,
        'audit_ts_low': audit_ts_low.isoformat(),
        'prev_state': prev_state,
        'dsn_validated': True,
    }

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={'note_key': 'k', 'success': False, 'count': 3},
        setup_context=setup_context,
    )

    unit = await _read_unit(env_dsn, unit_id)
    assert unit['failure_co_count'] == 0
    ue = await _read_unit_entity(env_dsn, unit_id)
    assert ue['failure_co_count'] == 0
    mm = await _read_mental_model(env_dsn, entity_id, vault_id)
    assert mm['failure_co_count'] == 0
    assert await _audit_count(env_dsn, unit_id, audit_ts_low) == 0
    api.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_two_record_outcome_actions_revert_independently(env_dsn: str) -> None:
    """End-to-end proof of the per-action context fix.

    Two ``record_outcome`` actions in one scenario — one stamps unit A
    with success, the other stamps unit B with failure. After running
    both teardowns through ``_run_setup_teardowns``, both units must
    be back to (0, 0) counters and both audit batches gone.

    Pre-fix: only the SECOND action's data survived in setup_context, so
    only one unit was reverted. Post-fix: each teardown reads its own
    per-action context.
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.base import SetupAction
    from memex_eval.suite.runner import _run_setup_teardowns

    vault_id = uuid4()
    unit_a = uuid4()
    unit_b = uuid4()
    entity_a = uuid4()
    entity_b = uuid4()

    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_a,
        entity_id=entity_a,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_b,
        entity_id=entity_b,
        success_co=0,
        failure_co=3,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_a, vault_id=vault_id, outcome='success', n=3)
    await _seed_audit_log(env_dsn, unit_id=unit_b, vault_id=vault_id, outcome='failure', n=3)

    actions = [
        SetupAction(kind='record_outcome', note_key='ach', success=True, count=3),
        SetupAction(kind='record_outcome', note_key='inc', success=False, count=3),
    ]

    # Build the same _per_action_results structure _run_setup_actions
    # would produce — each action's run() return is its own dict.
    setup_context = {
        '_executed_action_kinds': ['record_outcome'],
        '_executed_action_indices': [0, 1],
        '_per_action_results': [
            {
                'unit_ids': [str(unit_a)],
                'stamped_success': 3,
                'stamped_failure': 0,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_a): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
            {
                'unit_ids': [str(unit_b)],
                'stamped_success': 0,
                'stamped_failure': 3,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_b): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
        ],
    }

    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await _run_setup_teardowns(
        api=api,
        vault_id=vault_id,
        actions=actions,
        setup_context=setup_context,
    )

    # BOTH units back to (0, 0). Pre-fix, unit_a would still have
    # success_co=3 because both teardowns saw unit_b's context.
    a = await _read_unit(env_dsn, unit_a)
    b = await _read_unit(env_dsn, unit_b)
    assert a['success_co_count'] == 0, 'Unit A success not reverted'
    assert a['failure_co_count'] == 0
    assert b['success_co_count'] == 0
    assert b['failure_co_count'] == 0, 'Unit B failure not reverted'

    # Audit log fully purged for both units.
    assert await _audit_count(env_dsn, unit_a, audit_ts_low) == 0
    assert await _audit_count(env_dsn, unit_b, audit_ts_low) == 0

    api.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_record_outcome_teardown_noop_strategy_does_not_touch_db(
    env_dsn: str,
) -> None:
    """``teardown_strategy='noop'`` opts out — the seeded counters and
    audit_logs survive untouched."""
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_id, vault_id=vault_id, outcome='success', n=3)

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={
            'note_key': 'k',
            'success': True,
            'count': 3,
            'teardown_strategy': 'noop',
        },
        setup_context={
            'unit_ids': [str(unit_id)],
            'stamped_success': 3,
            'stamped_failure': 0,
            'audit_ts_low': audit_ts_low.isoformat(),
            'prev_state': {},
        },
    )

    # Nothing touched — DB still reads the seeded values.
    unit = await _read_unit(env_dsn, unit_id)
    assert unit['success_co_count'] == 3
    assert await _audit_count(env_dsn, unit_id, audit_ts_low) == 3
    api.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_record_outcome_teardown_only_deletes_its_own_audit_logs(
    env_dsn: str,
) -> None:
    """Teardown's ``DELETE FROM audit_logs WHERE timestamp >= audit_ts_low``
    must NOT touch audit rows that pre-date the run() call. Old audit
    history (e.g. from a prior scenario or an unrelated subsystem) stays
    intact."""
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()

    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )

    # Ancient audit row — timestamp set explicitly via update.
    conn = await asyncpg.connect(env_dsn)
    try:
        await conn.execute(
            'INSERT INTO audit_logs (id, action, resource_type, resource_id, details, timestamp) '
            "VALUES (gen_random_uuid(), 'outcome.record', 'memory_unit', $1, '{}'::jsonb, $2)",
            str(unit_id),
            datetime(2025, 1, 1, tzinfo=timezone.utc),  # old
        )
    finally:
        await conn.close()

    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_id, vault_id=vault_id, outcome='success', n=3)

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={'note_key': 'k', 'success': True, 'count': 3},
        setup_context={
            'unit_ids': [str(unit_id)],
            'stamped_success': 3,
            'stamped_failure': 0,
            'audit_ts_low': audit_ts_low.isoformat(),
            'prev_state': {
                str(unit_id): {
                    'success_co_count': 0,
                    'failure_co_count': 0,
                    'last_outcome_at': None,
                }
            },
            'dsn_validated': True,
        },
    )

    # The ancient row survives; the 3 fresh ones are gone.
    conn = await asyncpg.connect(env_dsn)
    try:
        total = await conn.fetchval(
            'SELECT COUNT(*) FROM audit_logs WHERE resource_id = $1',
            str(unit_id),
        )
        old_only = await conn.fetchval(
            'SELECT COUNT(*) FROM audit_logs WHERE resource_id = $1 AND timestamp < $2',
            str(unit_id),
            audit_ts_low,
        )
    finally:
        await conn.close()
    assert total == 1, 'expected only the ancient row to survive'
    assert old_only == 1


@pytest.mark.asyncio
async def test_runner_skips_teardown_for_failed_action_index(env_dsn: str) -> None:
    """``_run_setup_teardowns`` must skip teardown for an action whose
    run() raised — and must NOT use the merged-context fallback to re-run
    a sibling action's teardown (review round-1 CRITICAL #1, #2).

    Setup: action[0] succeeds (stamps unit A), action[1] raises.
    Expected: teardown[0] runs against unit A, teardown[1] is skipped.
    Pre-fix bug: the membership check fired teardown[1] anyway, and the
    merged-context fallback fed it action[0]'s data → unit A's counters
    were UPDATEd back to pre-state TWICE → no net wrong on the per-row
    UPDATE, but the propagation path's ``GREATEST(c - stamped, 0)`` ran
    twice on unit_entities/mental_models, double-decrementing them.
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.base import SetupAction
    from memex_eval.suite.runner import _run_setup_teardowns

    vault_id = uuid4()
    unit_a = uuid4()
    entity_a = uuid4()

    # Seed unit A at stamped state (success_co=3) and unit_entity/mental_model
    # also at +3. The teardown should bring them all back to (0,0).
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_a,
        entity_id=entity_a,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_a, vault_id=vault_id, outcome='success', n=3)

    actions = [
        SetupAction(kind='record_outcome', note_key='ach', success=True, count=3),
        SetupAction(kind='record_outcome', note_key='broken', success=True, count=3),
    ]
    setup_context = {
        # Only action[0]'s index is in executed_indices — action[1] failed.
        '_executed_action_kinds': ['record_outcome'],
        '_executed_action_indices': [0],
        '_per_action_results': [
            {
                'unit_ids': [str(unit_a)],
                'stamped_success': 3,
                'stamped_failure': 0,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_a): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
            None,  # action[1]'s run() raised
        ],
    }

    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)

    # Spy on the handler.teardown method to assert it was invoked EXACTLY
    # ONCE (for action[0]) — not twice (review round-2 MEDIUM #2). Without
    # this assertion the test cannot distinguish "action[1].teardown skipped"
    # from "action[1].teardown ran with empty unit_ids and early-returned".
    teardown_calls: list[dict[str, Any]] = []
    from memex_eval.suite.setup_actions import _RecordOutcome

    real_teardown = _RecordOutcome.teardown

    async def _spy_teardown(self, api, vault_id, params, setup_context):  # type: ignore[no-untyped-def]
        teardown_calls.append({'params': dict(params), 'setup_context': setup_context})
        return await real_teardown(self, api, vault_id, params, setup_context)

    from unittest.mock import patch

    with patch.object(_RecordOutcome, 'teardown', _spy_teardown):
        await _run_setup_teardowns(
            api=api,
            vault_id=vault_id,
            actions=actions,
            setup_context=setup_context,
        )

    # Strong assertion: exactly one teardown invocation — for action[0].
    assert len(teardown_calls) == 1, (
        f'expected exactly 1 record_outcome teardown call, got {len(teardown_calls)}: '
        f'{teardown_calls}'
    )
    # That call must have used action[0]'s data, NOT action[1]'s `broken`.
    assert teardown_calls[0]['params'].get('note_key') == 'ach'
    # And the setup_context passed in must be action[0]'s un-prefixed dict.
    assert teardown_calls[0]['setup_context'].get('unit_ids') == [str(unit_a)]

    # Unit A back to (0,0) — exactly one revert.
    a = await _read_unit(env_dsn, unit_a)
    assert a['success_co_count'] == 0
    # The DB-direct path was clean for action[0] — no flip-cancel.
    api.record_outcome.assert_not_called()
    # And: no audit_logs leftover for unit A.
    n_audit = await _audit_count(env_dsn, unit_a, audit_ts_low)
    assert n_audit == 0


@pytest.mark.asyncio
async def test_runner_skips_dsn_unvalidated_uses_flip_cancel_fallback(env_dsn: str) -> None:
    """When ``dsn_validated=False`` is in the per-action context (e.g. the
    DSN points at a wrong DB or pre-state capture failed), the teardown
    refuses the DB-direct path and uses API flip-cancel — proving the
    safety check actually gates the SQL (review round-1 HIGH #6).
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=3,
        failure_co=0,
        last_outcome_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_id, vault_id=vault_id, outcome='success', n=3)

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={'note_key': 'k', 'success': True, 'count': 3},
        setup_context={
            'unit_ids': [str(unit_id)],
            'stamped_success': 3,
            'stamped_failure': 0,
            'audit_ts_low': audit_ts_low.isoformat(),
            'prev_state': {
                str(unit_id): {
                    'success_co_count': 0,
                    'failure_co_count': 0,
                    'last_outcome_at': None,
                }
            },
            'dsn_validated': False,  # safety gate triggered
        },
    )
    # DB state untouched (precondition refused). Flip-cancel ran instead.
    unit = await _read_unit(env_dsn, unit_id)
    assert unit['success_co_count'] == 3, 'DB-direct path should have been refused'
    assert await _audit_count(env_dsn, unit_id, audit_ts_low) == 3
    assert api.record_outcome.call_count == 3
    for call in api.record_outcome.call_args_list:
        assert call.kwargs['success'] is False  # cancelling stamped success


@pytest.mark.asyncio
async def test_runner_skips_when_prev_state_partial_uses_flip_cancel_fallback(
    env_dsn: str,
) -> None:
    """If ``prev_state`` covers only some unit_ids, the safety check refuses
    the DB-direct path entirely (review round-1 MEDIUM #10). This avoids
    silently propagating a decrement to unit_entities / mental_models for
    units we don't have snapshots for — which would risk double-decrement
    on a retry.
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_id = uuid4()
    unit_a = uuid4()
    unit_b = uuid4()  # unit_b has no prev_state entry
    entity_a = uuid4()
    entity_b = uuid4()
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_a,
        entity_id=entity_a,
        success_co=3,
        failure_co=0,
        last_outcome_at=None,
    )
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_b,
        entity_id=entity_b,
        success_co=3,
        failure_co=0,
        last_outcome_at=None,
    )

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_id,
        params={'note_key': 'k', 'success': True, 'count': 3},
        setup_context={
            'unit_ids': [str(unit_a), str(unit_b)],
            'stamped_success': 3,
            'stamped_failure': 0,
            'audit_ts_low': datetime.now(timezone.utc).isoformat(),
            # Only unit_a has a snapshot — unit_b is missing.
            'prev_state': {
                str(unit_a): {
                    'success_co_count': 0,
                    'failure_co_count': 0,
                    'last_outcome_at': None,
                }
            },
            'dsn_validated': True,
        },
    )
    # Both units are still at (3,0): the SQL UPDATE never ran because
    # prev_state was partial. Flip-cancel ran instead via API.
    a = await _read_unit(env_dsn, unit_a)
    b = await _read_unit(env_dsn, unit_b)
    assert a['success_co_count'] == 3
    assert b['success_co_count'] == 3
    assert api.record_outcome.call_count == 3


@pytest.mark.asyncio
async def test_audit_delete_filters_by_vault_id(env_dsn: str) -> None:
    """The audit DELETE must NOT delete cross-vault rows that happen to
    share a unit_id (review round-1 MEDIUM #9). This is defense-in-depth
    — production schema enforces vault uniqueness on memory_units, but
    if that invariant ever weakens (or in a multi-vault eval environment
    where unit_ids could theoretically collide), the teardown must scope
    to the action's own vault.
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.setup_actions import get_setup_action

    vault_a = uuid4()
    vault_b = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()

    # Seed the unit in vault A.
    await _seed_unit(
        env_dsn,
        vault_id=vault_a,
        note_id=uuid4(),
        unit_id=unit_id,
        entity_id=entity_id,
        success_co=3,
        failure_co=0,
        last_outcome_at=None,
    )
    audit_ts_low = await _pg_now(env_dsn)
    # Insert audit rows attributed to vault B sharing the same unit_id.
    conn = await asyncpg.connect(env_dsn)
    try:
        for _ in range(3):
            await conn.execute(
                'INSERT INTO audit_logs (id, action, resource_type, resource_id, details) '
                "VALUES (gen_random_uuid(), 'outcome.record', 'memory_unit', $1, $2::jsonb)",
                str(unit_id),
                f'{{"vault_id": "{vault_b}", "outcome": "success"}}',
            )
        # And 3 audit rows attributed to vault A — these SHOULD be deleted.
        for _ in range(3):
            await conn.execute(
                'INSERT INTO audit_logs (id, action, resource_type, resource_id, details) '
                "VALUES (gen_random_uuid(), 'outcome.record', 'memory_unit', $1, $2::jsonb)",
                str(unit_id),
                f'{{"vault_id": "{vault_a}", "outcome": "success"}}',
            )
    finally:
        await conn.close()

    handler = get_setup_action('record_outcome')
    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    await handler.teardown(
        api=api,
        vault_id=vault_a,
        params={'note_key': 'k', 'success': True, 'count': 3},
        setup_context={
            'unit_ids': [str(unit_id)],
            'stamped_success': 3,
            'stamped_failure': 0,
            'audit_ts_low': audit_ts_low.isoformat(),
            'prev_state': {
                str(unit_id): {
                    'success_co_count': 0,
                    'failure_co_count': 0,
                    'last_outcome_at': None,
                }
            },
            'dsn_validated': True,
        },
    )

    # Vault A's 3 rows are gone. Vault B's 3 rows survive.
    conn = await asyncpg.connect(env_dsn)
    try:
        a_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_logs WHERE resource_id = $1 AND details->>'vault_id' = $2",
            str(unit_id),
            str(vault_a),
        )
        b_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_logs WHERE resource_id = $1 AND details->>'vault_id' = $2",
            str(unit_id),
            str(vault_b),
        )
    finally:
        await conn.close()
    assert a_count == 0, 'expected vault A audit rows to be deleted'
    assert b_count == 3, 'vault B audit rows must NOT be deleted'


@pytest.mark.asyncio
async def test_runner_with_mixed_kinds_isolates_each_teardown(env_dsn: str) -> None:
    """End-to-end through ``_run_setup_teardowns`` with TWO different
    handler kinds (record_outcome + kv_write) plus a third record_outcome
    on a different unit. Each teardown must see its own per-action context;
    none should bleed into another (review round-1 MEDIUM #13).
    """
    from unittest.mock import AsyncMock, MagicMock

    from memex_eval.suite.base import SetupAction
    from memex_eval.suite.runner import _run_setup_teardowns

    vault_id = uuid4()
    unit_a = uuid4()
    unit_c = uuid4()
    entity_a = uuid4()
    entity_c = uuid4()

    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_a,
        entity_id=entity_a,
        success_co=3,
        failure_co=0,
        last_outcome_at=None,
    )
    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_c,
        entity_id=entity_c,
        success_co=2,
        failure_co=0,
        last_outcome_at=None,
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_a, vault_id=vault_id, outcome='success', n=3)
    await _seed_audit_log(env_dsn, unit_id=unit_c, vault_id=vault_id, outcome='success', n=2)

    actions = [
        SetupAction(kind='record_outcome', note_key='a', success=True, count=3),
        SetupAction(kind='kv_write', kv_key='project:demo:flag', kv_value='on'),
        SetupAction(kind='record_outcome', note_key='c', success=True, count=2),
    ]
    setup_context = {
        '_executed_action_kinds': ['record_outcome', 'kv_write'],
        '_executed_action_indices': [0, 1, 2],
        '_per_action_results': [
            {
                'unit_ids': [str(unit_a)],
                'stamped_success': 3,
                'stamped_failure': 0,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_a): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
            {'kv_key': 'project:demo:flag'},
            {
                'unit_ids': [str(unit_c)],
                'stamped_success': 2,
                'stamped_failure': 0,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_c): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
        ],
    }

    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)
    api.kv_delete = AsyncMock(return_value=None)
    await _run_setup_teardowns(
        api=api,
        vault_id=vault_id,
        actions=actions,
        setup_context=setup_context,
    )

    # Both units back to (0,0).
    a = await _read_unit(env_dsn, unit_a)
    c = await _read_unit(env_dsn, unit_c)
    assert a['success_co_count'] == 0, 'unit A not reverted (mixed-kind iso failed)'
    assert c['success_co_count'] == 0, 'unit C not reverted (mixed-kind iso failed)'
    # KV teardown fired with the right key.
    api.kv_delete.assert_called_once_with(key='project:demo:flag')
    # All audit rows for both units gone.
    assert await _audit_count(env_dsn, unit_a, audit_ts_low) == 0
    assert await _audit_count(env_dsn, unit_c, audit_ts_low) == 0
    # No flip-cancel needed — the DB-direct path took both record_outcome
    # teardowns to completion.
    api.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_runner_with_required_break_skips_unrun_action_teardowns(env_dsn: str) -> None:
    """When a required setup_action raises and the runner ``break``s mid-list,
    later actions never execute. Their teardowns must NOT run, even though
    ``_per_action_results`` is shorter than ``actions`` (review round-2
    MEDIUM #6).

    Setup: 4 actions where action[1] is required and raises. The runner
    populates `_executed_action_indices=[0]` and `_per_action_results=[
    {action[0] data}, None]` (length 2, not 4). Teardown loop iterates all
    4 actions but must skip indices 1, 2, 3 — without IndexError on the
    bounds check for the slot lookup.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from memex_eval.suite.base import SetupAction
    from memex_eval.suite.runner import _run_setup_teardowns

    vault_id = uuid4()
    unit_a = uuid4()
    entity_a = uuid4()

    await _seed_unit(
        env_dsn,
        vault_id=vault_id,
        note_id=uuid4(),
        unit_id=unit_a,
        entity_id=entity_a,
        success_co=3,
        failure_co=0,
        last_outcome_at=None,
    )
    audit_ts_low = await _pg_now(env_dsn)
    await _seed_audit_log(env_dsn, unit_id=unit_a, vault_id=vault_id, outcome='success', n=3)

    actions = [
        SetupAction(kind='record_outcome', note_key='a', success=True, count=3),
        SetupAction(kind='record_outcome', note_key='b-required-fail', required=True),
        SetupAction(kind='record_outcome', note_key='c-never-runs'),
        SetupAction(kind='record_outcome', note_key='d-never-runs'),
    ]
    # The runner state after a required-break: only action[0] succeeded.
    # _per_action_results is length 2 (one per attempted action), shorter
    # than the actions list (length 4).
    setup_context = {
        '_executed_action_kinds': ['record_outcome'],
        '_executed_action_indices': [0],
        '_per_action_results': [
            {
                'unit_ids': [str(unit_a)],
                'stamped_success': 3,
                'stamped_failure': 0,
                'audit_ts_low': audit_ts_low.isoformat(),
                'prev_state': {
                    str(unit_a): {
                        'success_co_count': 0,
                        'failure_co_count': 0,
                        'last_outcome_at': None,
                    }
                },
                'dsn_validated': True,
            },
            None,  # action[1] raised
            # action[2], action[3]: never iterated, no slot in this list.
        ],
        '_required_setup_failed': True,
    }

    api = MagicMock()
    api.record_outcome = AsyncMock(return_value=None)

    teardown_calls: list[dict[str, Any]] = []
    from memex_eval.suite.setup_actions import _RecordOutcome

    real_teardown = _RecordOutcome.teardown

    async def _spy_teardown(self, api, vault_id, params, setup_context):  # type: ignore[no-untyped-def]
        teardown_calls.append({'params': dict(params), 'setup_context': setup_context})
        return await real_teardown(self, api, vault_id, params, setup_context)

    with patch.object(_RecordOutcome, 'teardown', _spy_teardown):
        # Crucially: must NOT raise IndexError on the bounds check for
        # actions[2] and actions[3] when slot lookup would go past the
        # end of the (length-2) _per_action_results list.
        await _run_setup_teardowns(
            api=api,
            vault_id=vault_id,
            actions=actions,
            setup_context=setup_context,
        )

    # Exactly ONE teardown invocation, for action[0].
    assert len(teardown_calls) == 1
    assert teardown_calls[0]['params'].get('note_key') == 'a'
    # Unit A reverted; no flip-cancel.
    a = await _read_unit(env_dsn, unit_a)
    assert a['success_co_count'] == 0
    api.record_outcome.assert_not_called()
    # Audit rows for action[0]'s unit gone.
    assert await _audit_count(env_dsn, unit_a, audit_ts_low, vault_id) == 0
