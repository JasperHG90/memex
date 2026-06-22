"""Migration round-trip test for dropping the orphan webhook_* tables (068).

Verifies via a real ``alembic upgrade``/``downgrade`` (not create_all) that:
- The webhook tables exist at 067 (created at 001, untouched since).
- Upgrading 067 -> 068 drops both ``webhook_registrations`` and
  ``webhook_deliveries``.
- Downgrade 068 -> 067 recreates both tables and their three indexes, so the
  migration is reversible.
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

_TARGET_BEFORE = '067_add_skill_hints'
_TARGET_AFTER = '068_drop_webhook_tables'

_WEBHOOK_TABLES = ('webhook_registrations', 'webhook_deliveries')
_WEBHOOK_INDEXES = (
    'idx_webhook_registrations_active',
    'idx_webhook_deliveries_webhook_id',
    'idx_webhook_deliveries_status',
)


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig068'):
        yield url


async def _existing(conn, names: tuple[str, ...], relkind: str) -> set[str]:
    # pg_class.relkind is Postgres's ``"char"`` type; cast to text so a plain
    # str param compares cleanly (asyncpg can't encode str into a "char" array).
    rows = (
        await conn.execute(
            text(
                'SELECT relname FROM pg_class WHERE relname = ANY(:names) AND relkind::text = :kind'
            ),
            {'names': list(names), 'kind': relkind},
        )
    ).all()
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_upgrade_drops_webhook_tables(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        # Sanity: both webhook tables exist at 067 (created in 001, never touched).
        async with engine.connect() as conn:
            assert await _existing(conn, _WEBHOOK_TABLES, 'r') == set(_WEBHOOK_TABLES)

        await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

        async with engine.connect() as conn:
            survivors = await _existing(conn, _WEBHOOK_TABLES, 'r')
            assert survivors == set(), f'webhook tables survived upgrade-to-068: {survivors}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_recreates_webhook_tables(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = await _existing(conn, _WEBHOOK_TABLES, 'r')
            assert tables == set(_WEBHOOK_TABLES), f'webhook tables not recreated: {tables}'
            indexes = await _existing(conn, _WEBHOOK_INDEXES, 'i')
            assert indexes == set(_WEBHOOK_INDEXES), f'webhook indexes not recreated: {indexes}'
    finally:
        await engine.dispose()
