"""Migration round-trip test for the nodes/chunks stored tsvector (069).

Verifies via a real ``alembic upgrade``/``downgrade`` (not create_all) that:
- At 068, nodes/chunks have the functional GIN index and NO search_tsvector column.
- Upgrading 068 -> 069 adds the generated ``search_tsvector`` column to both tables,
  creates the new GIN indexes on it, and drops the old functional GIN indexes.
- Downgrade 069 -> 068 drops the columns + new indexes and restores the functional
  indexes, so the migration is reversible.

(Generated-column backfill of existing rows is a Postgres guarantee and is
exercised end-to-end by the document_search integration suite, which keyword-
searches on the populated column.)
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import (  # noqa: F401
    alembic_downgrade as _alembic_downgrade,
    alembic_upgrade as _alembic_upgrade,
    make_fresh_db,
)

pytestmark = [pytest.mark.integration]

_TARGET_BEFORE = '068_drop_webhook_tables'
_TARGET_AFTER = '069_nodes_chunks_search_tsvector'

_OLD_INDEXES = ('idx_nodes_text_tsvector', 'idx_chunks_text_tsvector')
_NEW_INDEXES = ('idx_nodes_search_tsvector', 'idx_chunks_search_tsvector')


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig069'):
        yield url


async def _indexes(conn, names: tuple[str, ...]) -> set[str]:
    rows = (
        await conn.execute(
            text(
                "SELECT relname FROM pg_class WHERE relname = ANY(:names) AND relkind::text = 'i'"
            ),
            {'names': list(names)},
        )
    ).all()
    return {r[0] for r in rows}


async def _has_search_tsvector(conn, table: str) -> bool:
    row = (
        await conn.execute(
            text(
                'SELECT 1 FROM information_schema.columns '
                'WHERE table_name = :t AND column_name = :c'
            ),
            {'t': table, 'c': 'search_tsvector'},
        )
    ).first()
    return row is not None


@pytest.mark.asyncio
async def test_upgrade_adds_stored_tsvector_and_swaps_index(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _indexes(conn, _OLD_INDEXES) == set(_OLD_INDEXES)
            assert await _indexes(conn, _NEW_INDEXES) == set()
            assert not await _has_search_tsvector(conn, 'nodes')
            assert not await _has_search_tsvector(conn, 'chunks')

        await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

        async with engine.connect() as conn:
            assert await _has_search_tsvector(conn, 'nodes')
            assert await _has_search_tsvector(conn, 'chunks')
            assert await _indexes(conn, _NEW_INDEXES) == set(_NEW_INDEXES)
            survivors = await _indexes(conn, _OLD_INDEXES)
            assert survivors == set(), f'functional indexes survived upgrade-to-069: {survivors}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_restores_functional_index(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert not await _has_search_tsvector(conn, 'nodes')
            assert not await _has_search_tsvector(conn, 'chunks')
            assert await _indexes(conn, _NEW_INDEXES) == set()
            restored = await _indexes(conn, _OLD_INDEXES)
            assert restored == set(_OLD_INDEXES), f'functional indexes not restored: {restored}'
    finally:
        await engine.dispose()
