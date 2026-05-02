"""TC-23-2 — acquire_entity_lock lifecycle (F9 ship, locked v2 contract).

5 tests:
- test_acquire_release_visible_in_pg_locks: lock visible during yield, gone after.
- test_split_matches_actual_pg_locks_layout: empirical column-placement check.
- test_timeout_raises_entity_lock_timeout_error: bounded wait, typed exception.
- test_cancelled_acquire_releases_lock: no leak on task cancellation mid-spin.
- test_acquire_survives_sa_session_lifecycle: SA session open/close inside the
  body does not release the lock (RFC-005 connection-per-acquire load-bearing).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from memex_core.services.locks import (
    EntityLockTimeoutError,
    acquire_entity_lock,
    entity_lock_id,
    split_for_pg_locks,
)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_PG_LOCKS_QUERY = (
    'SELECT pid, granted FROM pg_locks '
    "WHERE locktype = 'advisory' "
    'AND classid = $1 AND objid = $2 AND objsubid = 1'
)


@pytest.fixture(scope='module')
def asyncpg_dsn(postgres_container: PostgresContainer) -> str:
    """Plain postgresql:// DSN for asyncpg.connect (strips +psycopg2 suffix)."""
    url = postgres_container.get_connection_url().replace('postgresql+psycopg2://', 'postgresql://')
    parsed = urlparse(url)
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


@pytest_asyncio.fixture
async def helper_conn(asyncpg_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    """A side-channel asyncpg connection for asserting against pg_locks."""
    conn = await asyncpg.connect(asyncpg_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_acquire_release_visible_in_pg_locks(
    asyncpg_dsn: str, helper_conn: asyncpg.Connection
) -> None:
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)
    split = split_for_pg_locks(lock_id)

    async with acquire_entity_lock(asyncpg_dsn, eid):
        rows = await helper_conn.fetch(_PG_LOCKS_QUERY, split.classid, split.objid)
        assert len(rows) == 1, f'expected 1 advisory-lock row, got {len(rows)}'
        assert rows[0]['granted'] is True

    rows = await helper_conn.fetch(_PG_LOCKS_QUERY, split.classid, split.objid)
    assert rows == [], f'lock not released — pg_locks still shows {rows}'


async def test_split_matches_actual_pg_locks_layout(
    asyncpg_dsn: str, helper_conn: asyncpg.Connection
) -> None:
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)
    split = split_for_pg_locks(lock_id)

    async with acquire_entity_lock(asyncpg_dsn, eid):
        rows = await helper_conn.fetch(
            "SELECT classid, objid, objsubid FROM pg_locks WHERE locktype = 'advisory'"
        )
        match = [r for r in rows if r['classid'] == split.classid and r['objid'] == split.objid]
        assert len(match) == 1, (
            f'split_for_pg_locks layout disagrees with Postgres — '
            f'expected (classid={split.classid}, objid={split.objid}) in pg_locks, '
            f'observed advisory rows: {list(rows)}'
        )
        assert match[0]['objsubid'] == 1, (
            f'objsubid must be 1 for single-int advisory locks (got {match[0]["objsubid"]})'
        )


async def test_timeout_raises_entity_lock_timeout_error(
    asyncpg_dsn: str, helper_conn: asyncpg.Connection
) -> None:
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)

    got = await helper_conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
    assert got is True, 'helper failed to grab the lock for the contention setup'

    try:
        with pytest.raises(EntityLockTimeoutError) as exc_info:
            async with acquire_entity_lock(asyncpg_dsn, eid, timeout_seconds=0.5):
                pytest.fail('acquire should have timed out, body must not run')
        assert str(eid) in str(exc_info.value)
    finally:
        await helper_conn.execute('SELECT pg_advisory_unlock($1)', lock_id)


async def test_cancelled_acquire_releases_lock(
    asyncpg_dsn: str, helper_conn: asyncpg.Connection
) -> None:
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)
    split = split_for_pg_locks(lock_id)

    got = await helper_conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
    assert got is True

    async def _block_on_acquire() -> None:
        async with acquire_entity_lock(asyncpg_dsn, eid, timeout_seconds=10.0):
            pytest.fail('body should not run; the task is cancelled while spinning')

    task = asyncio.create_task(_block_on_acquire())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await helper_conn.execute('SELECT pg_advisory_unlock($1)', lock_id)

    async with acquire_entity_lock(asyncpg_dsn, eid, timeout_seconds=2.0):
        rows = await helper_conn.fetch(_PG_LOCKS_QUERY, split.classid, split.objid)
        assert len(rows) == 1, (
            f'after cancellation + helper release, the next acquirer must succeed; '
            f'pg_locks scoped to lock_id={lock_id} shows {rows}'
        )


async def test_acquire_survives_sa_session_lifecycle(
    asyncpg_dsn: str, helper_conn: asyncpg.Connection, postgres_uri: str
) -> None:
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)
    split = split_for_pg_locks(lock_id)

    async with acquire_entity_lock(asyncpg_dsn, eid):
        engine = create_async_engine(postgres_uri)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sessionmaker() as session:
            row = (await session.execute(text('SELECT 1'))).scalar()
            assert row == 1
        await engine.dispose()

        rows = await helper_conn.fetch(_PG_LOCKS_QUERY, split.classid, split.objid)
        assert len(rows) == 1, (
            f'advisory lock disappeared after SA session lifecycle — pg_locks={rows}. '
            'RFC-005 connection-per-acquire design depends on this not happening.'
        )
        assert rows[0]['granted'] is True
