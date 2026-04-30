"""TC-12-1 — basic acquire/release semantics on a dedicated asyncpg connection.

Validates RFC-005's foundational claim: pg_try_advisory_lock acquires, the lock
is observable in pg_locks scoped by lock_id (TS refinement #1), and
pg_advisory_unlock releases on the same connection.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from _helpers import PG_LOCKS_QUERY, entity_lock_id, split_lock_id_for_pg_locks


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_acquire_then_release(asyncpg_dsn: str) -> None:
    lock_id = entity_lock_id(uuid.uuid4())
    classid, objid = split_lock_id_for_pg_locks(lock_id)

    conn = await asyncpg.connect(asyncpg_dsn)
    try:
        got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
        assert got is True, 'pg_try_advisory_lock should acquire on idle DB'

        rows = await conn.fetch(PG_LOCKS_QUERY, classid, objid)
        assert len(rows) == 1, f'expected 1 advisory-lock row scoped to lock_id, got {len(rows)}'
        assert rows[0]['granted'] is True

        released = await conn.fetchval('SELECT pg_advisory_unlock($1)', lock_id)
        assert released is True, 'pg_advisory_unlock should return true on release'

        rows = await conn.fetch(PG_LOCKS_QUERY, classid, objid)
        assert rows == [], f'lock not released — pg_locks still shows {rows}'
    finally:
        await conn.close()
