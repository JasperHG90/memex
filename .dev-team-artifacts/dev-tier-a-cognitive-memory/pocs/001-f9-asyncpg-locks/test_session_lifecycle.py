"""TC-12-2 — lock survives SQLAlchemy session close.

The load-bearing claim of RFC-005's connection-per-acquire design: holding an
advisory lock on a dedicated asyncpg connection must NOT be released when an
unrelated SQLAlchemy AsyncSession (drawn from the SA pool) opens and closes.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from _helpers import PG_LOCKS_QUERY, entity_lock_id, split_lock_id_for_pg_locks


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_lock_survives_sqlalchemy_session_close(
    asyncpg_dsn: str, sqla_async_url: str
) -> None:
    lock_id = entity_lock_id(uuid.uuid4())
    classid, objid = split_lock_id_for_pg_locks(lock_id)

    holder = await asyncpg.connect(asyncpg_dsn)
    try:
        got = await holder.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
        assert got is True

        engine = create_async_engine(sqla_async_url)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sessionmaker() as session:
            row = (await session.execute(text('SELECT 1'))).scalar()
            assert row == 1
        await engine.dispose()

        rows = await holder.fetch(PG_LOCKS_QUERY, classid, objid)
        assert len(rows) == 1, (
            f'advisory lock disappeared after SA session lifecycle — pg_locks={rows}. '
            'RFC-005 connection-per-acquire design depends on this not happening.'
        )
        assert rows[0]['granted'] is True
        assert rows[0]['pid'] == await holder.fetchval('SELECT pg_backend_pid()')

        released = await holder.fetchval('SELECT pg_advisory_unlock($1)', lock_id)
        assert released is True
    finally:
        await holder.close()
