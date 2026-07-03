"""F35 — alembic 034_add_mw_mode migration tests.

Verifies the ``mw_mode`` column on ``vaults`` with the CHECK constraint
``mw_mode IN ('stationary', 'ema')``, plus the upgrade -> downgrade ->
upgrade round-trip on a real Postgres container.
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

_TARGET = '034_add_mw_mode'
_DOWN = '033_confidence_evidence_count'
_COLUMN = 'mw_mode'
_CHECK = 'vaults_mw_mode_check'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig034'):
        yield url


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_column_and_check(fresh_db_url: str) -> None:
    """Upgrade adds NOT NULL ``mw_mode`` (default 'stationary') and the
    ``mw_mode IN ('stationary', 'ema')`` CHECK constraint."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col_row = (
                await conn.execute(
                    text(
                        'SELECT data_type, is_nullable, column_default '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'vaults' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).first()
            assert col_row is not None, f'column {_COLUMN!r} missing after upgrade'
            data_type, is_nullable, column_default = col_row
            assert data_type == 'text', f'unexpected type {data_type!r}'
            assert is_nullable == 'NO', f'column should be NOT NULL, got {is_nullable!r}'
            assert column_default is not None and 'stationary' in column_default, (
                f"column default should contain 'stationary', got {column_default!r}"
            )

            check_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.table_constraints '
                        "WHERE constraint_name = :name AND constraint_type = 'CHECK'"
                    ),
                    {'name': _CHECK},
                )
            ).scalar()
            assert check_exists == 1, f'expected CHECK constraint {_CHECK!r} after upgrade'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_value(fresh_db_url: str) -> None:
    """The CHECK constraint rejects invalid mw_mode values at the DB level."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("INSERT INTO vaults (id, name, mw_mode) VALUES (:id, :name, 'stationary')"),
                {'id': '11111111-1111-1111-1111-111111111111', 'name': 'valid_vault'},
            )
            await conn.commit()

            with pytest.raises(Exception, match='violates check constraint'):
                await conn.execute(
                    text("INSERT INTO vaults (id, name, mw_mode) VALUES (:id, :name, 'emma')"),
                    {'id': '22222222-2222-2222-2222-222222222222', 'name': 'bad_vault'},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_downgrade_drops_column_and_check(fresh_db_url: str) -> None:
    """Downgrade past 034 removes the column and CHECK constraint."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'vaults' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar()
            assert col_exists is None, f'column {_COLUMN!r} should be gone after downgrade'

            check_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.table_constraints '
                        "WHERE constraint_name = :name AND constraint_type = 'CHECK'"
                    ),
                    {'name': _CHECK},
                )
            ).scalar()
            assert check_exists is None, (
                f'CHECK constraint {_CHECK!r} should be gone after downgrade'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_round_trip(fresh_db_url: str) -> None:
    """upgrade -> downgrade -> upgrade on a live DB."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'vaults' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() == 1
    finally:
        await engine.dispose()

    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine2 = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine2.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'vaults' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() is None
    finally:
        await engine2.dispose()

    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine3 = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine3.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'vaults' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() == 1
    finally:
        await engine3.dispose()
