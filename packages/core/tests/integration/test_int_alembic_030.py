"""F20 — alembic 030_revisit_last_reviewed_at migration tests.

Verifies the ``revisit_last_reviewed_at`` column is created on
``memory_units`` (TIMESTAMPTZ, NULLABLE) and that the upgrade/downgrade
pair is reversible. Companion to ``test_int_alembic_026.py``.
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


_TARGET = '030_revisit_last_reviewed_at'
_DOWN = '029_lint_llm_quota'
_COLUMN = 'revisit_last_reviewed_at'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig030'):
        yield url


@pytest.mark.asyncio
async def test_alembic_upgrade_adds_revisit_last_reviewed_at(fresh_db_url: str) -> None:
    """Upgrade adds nullable ``revisit_last_reviewed_at`` TIMESTAMPTZ column."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).first()
            assert row is not None, f'column {_COLUMN!r} missing after upgrade'
            data_type, is_nullable = row
            assert data_type.startswith('timestamp'), f'unexpected type {data_type!r}'
            assert is_nullable == 'YES', f'column should be nullable, got {is_nullable!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_downgrade_drops_revisit_last_reviewed_at(fresh_db_url: str) -> None:
    """Downgrade past 030 removes the column."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar()
            assert exists is None, f'column {_COLUMN!r} should be gone after downgrade'
    finally:
        await engine.dispose()
