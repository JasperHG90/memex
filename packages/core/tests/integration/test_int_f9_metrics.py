"""TC-23-6 — F9 Prometheus counter exposure (AC-X-9).

4 tests verify the F9 counters increment as expected:
- entity_lock_acquires_total{outcome="acquired"} after a successful reconsolidate
- entity_lock_acquires_total{outcome="timeout"} when the lock is held externally
- reconsolidate_total{outcome="success"} on success
- consolidate_total{outcome="success"} on dry_run

We read counter samples directly via the Prometheus client registry rather
than scraping /metrics, since /metrics requires a running uvicorn — these
tests run in-process and instrument LocksService directly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession
from testcontainers.postgres import PostgresContainer

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
from memex_core.metrics import (
    CONSOLIDATE_TOTAL,
    ENTITY_LOCK_ACQUIRES_TOTAL,
    RECONSOLIDATE_TOTAL,
)
from memex_core.services.locks import EntityLockTimeoutError, LocksService, entity_lock_id
from memex_core.services.reflection import ReflectionService

pytestmark = [pytest.mark.integration, pytest.mark.llm_mock]


def _counter_value(metric, **labels: str) -> float:
    """Read the current value of a labelled Counter via the registry."""
    return metric.labels(**labels)._value.get()


@pytest.fixture(scope='module')
def asyncpg_dsn(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url().replace('postgresql+psycopg2://', 'postgresql://')
    parsed = urlparse(url)
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


@pytest_asyncio.fixture
async def helper_conn(asyncpg_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    conn = await asyncpg.connect(asyncpg_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def metrics_locks_service(metastore, memex_config, asyncpg_dsn) -> LocksService:
    contradiction = MagicMock(spec=ContradictionEngine)
    contradiction.detect_contradictions = AsyncMock(return_value=None)

    reflection = MagicMock(spec=ReflectionService)
    reflection.reflect_batch = AsyncMock(return_value=[])

    svc = LocksService.__new__(LocksService)
    svc.metastore = metastore
    svc.config = memex_config
    svc.reflection = reflection
    svc.contradiction = contradiction
    svc.units = None  # consolidate dry_run does not need units
    svc._dsn = asyncpg_dsn
    # ``__new__`` bypasses ``__init__``, so the lazy-init attributes that
    # ``_get_pool`` and ``consolidate_vault`` read must be set explicitly.
    svc._pool = None
    svc._has_maintenance_proposals_table_cache = None
    # Defensive: tests in this file only exercise dry_run consolidate +
    # reconsolidate paths (neither touches the limiter), but match the
    # full ``__init__`` shape so future test additions don't trip on a
    # missing attribute.
    svc._consolidate_limiter = None
    return svc


async def _seed_entity(session: AsyncSession, vault_id: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    note_id = uuid.uuid4()
    session.add(Note(id=note_id, content_hash='abc', vault_id=vault_id, original_text='seed'))
    session.add(
        Entity(
            id=eid,
            canonical_name=f'Entity-{eid.hex[:6]}',
            entity_type='other',
            description='F9 metrics test entity',
        )
    )
    uid = uuid.uuid4()
    session.add(
        MemoryUnit(
            id=uid,
            note_id=note_id,
            text='seed unit',
            fact_type='world',
            vault_id=vault_id,
            embedding=[0.1] * 384,
            event_date=datetime.now(timezone.utc),
        )
    )
    session.add(UnitEntity(unit_id=uid, entity_id=eid, vault_id=vault_id))
    await session.commit()
    return eid


async def test_acquires_counter_increments_on_success(
    metrics_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    eid = await _seed_entity(session, GLOBAL_VAULT_ID)
    before = _counter_value(ENTITY_LOCK_ACQUIRES_TOTAL, outcome='acquired')
    await metrics_locks_service.reconsolidate_entity(eid, GLOBAL_VAULT_ID)
    after = _counter_value(ENTITY_LOCK_ACQUIRES_TOTAL, outcome='acquired')
    assert after - before == 1, 'acquired counter must increment by 1 on successful lock'


async def test_acquires_counter_increments_on_timeout(
    metrics_locks_service: LocksService,
    session: AsyncSession,
    helper_conn: asyncpg.Connection,
) -> None:
    eid = await _seed_entity(session, GLOBAL_VAULT_ID)
    lock_id = entity_lock_id(eid)
    got = await helper_conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
    assert got is True
    try:
        before = _counter_value(ENTITY_LOCK_ACQUIRES_TOTAL, outcome='timeout')
        with pytest.raises(EntityLockTimeoutError):
            await metrics_locks_service.reconsolidate_entity(
                eid, GLOBAL_VAULT_ID, timeout_seconds=0.3
            )
        after = _counter_value(ENTITY_LOCK_ACQUIRES_TOTAL, outcome='timeout')
        assert after - before == 1, 'timeout counter must increment by 1 on lock failure'
    finally:
        await helper_conn.execute('SELECT pg_advisory_unlock($1)', lock_id)


async def test_reconsolidate_total_increments_on_success(
    metrics_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    eid = await _seed_entity(session, GLOBAL_VAULT_ID)
    before_success = _counter_value(RECONSOLIDATE_TOTAL, outcome='success')
    before_timeout = _counter_value(RECONSOLIDATE_TOTAL, outcome='lock_timeout')
    await metrics_locks_service.reconsolidate_entity(eid, GLOBAL_VAULT_ID)
    after_success = _counter_value(RECONSOLIDATE_TOTAL, outcome='success')
    after_timeout = _counter_value(RECONSOLIDATE_TOTAL, outcome='lock_timeout')
    assert after_success - before_success == 1
    assert after_timeout - before_timeout == 0


async def test_consolidate_total_increments_on_dry_run(
    metrics_locks_service: LocksService,
) -> None:
    before = _counter_value(CONSOLIDATE_TOTAL, outcome='success')
    await metrics_locks_service.consolidate_vault(GLOBAL_VAULT_ID, dry_run=True)
    after = _counter_value(CONSOLIDATE_TOTAL, outcome='success')
    assert after - before == 1, 'consolidate dry_run must increment success counter'
