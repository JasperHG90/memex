"""Integration tests for migration 062_notes_role.

Verifies via real ``alembic upgrade`` / ``downgrade`` against a Postgres
testcontainer:

- 062 adds a nullable ``role`` column to ``notes`` with a CHECK
  constraint limiting the values to NULL, 'case', 'procedure', or
  'strategy';
- the partial index ``idx_notes_role`` covers only the non-NULL rows;
- inserting an invalid value (e.g. 'plan') is rejected at the DB layer;
- downgrade drops both the column and the partial index.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import (  # noqa: F401
    alembic_downgrade as _alembic_downgrade,
    alembic_upgrade as _alembic_upgrade,
    make_fresh_db,
)

pytestmark = [pytest.mark.integration]

_TARGET = '062_notes_role'
_DOWN = '061_experiential_entries'
_COLUMN = 'role'
_CHECK = 'ck_notes_role'
_INDEX = 'idx_notes_role'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig062'):
        yield url


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    'SELECT 1 FROM information_schema.columns '
                    'WHERE table_name = :t AND column_name = :c'
                ),
                {'t': table, 'c': column},
            )
        ).scalar()
    )


async def _check_exists(conn, name: str) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    'SELECT 1 FROM information_schema.table_constraints '
                    "WHERE constraint_name = :n AND constraint_type = 'CHECK'"
                ),
                {'n': name},
            )
        ).scalar()
    )


async def _index_exists(conn, name: str) -> bool:
    return bool(
        (
            await conn.execute(
                text('SELECT 1 FROM pg_indexes WHERE indexname = :n'),
                {'n': name},
            )
        ).scalar()
    )


@pytest.mark.asyncio
async def test_upgrade_adds_column_check_and_partial_index(fresh_db_url: str) -> None:
    """After upgrade, ``notes.role`` is TEXT NULL, with the role CHECK
    constraint and the partial index on (vault_id, role) WHERE role IS NOT NULL."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col_row = (
                await conn.execute(
                    text(
                        'SELECT data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'notes' AND column_name = :c"
                    ),
                    {'c': _COLUMN},
                )
            ).first()
            assert col_row is not None, f'column {_COLUMN!r} missing on notes'
            data_type, is_nullable = col_row
            assert data_type == 'text', f'expected TEXT column, got {data_type!r}'
            assert is_nullable == 'YES', f'expected NULLABLE column, got {is_nullable!r}'

            assert await _check_exists(conn, _CHECK), (
                f'expected CHECK constraint {_CHECK!r} after 062 upgrade'
            )
            assert await _index_exists(conn, _INDEX), (
                f'expected partial index {_INDEX!r} after 062 upgrade'
            )
            indexdef = (
                await conn.execute(
                    text('SELECT indexdef FROM pg_indexes WHERE indexname = :n'),
                    {'n': _INDEX},
                )
            ).scalar()
            assert indexdef is not None, f'could not read indexdef for {_INDEX!r}'
            assert 'role IS NOT NULL' in indexdef, (
                f'expected partial predicate role IS NOT NULL on {_INDEX!r}, got {indexdef!r}'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_accepts_allowed_values_and_rejects_unknown(
    fresh_db_url: str,
) -> None:
    """The CHECK constraint allows 'case', 'procedure', 'strategy' and
    rejects any other non-NULL value."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for value in ('case', 'procedure', 'strategy'):
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name) VALUES (gen_random_uuid(), :n) RETURNING id'
                    ),
                    {'n': f'mig062-{value}-vault'},
                )
                # Each vault gets one note stamped with the role value.
                await conn.execute(
                    text(
                        'INSERT INTO notes (id, vault_id, role) '
                        'VALUES (gen_random_uuid(), '
                        '  (SELECT id FROM vaults WHERE name = :n), :r)'
                    ),
                    {'n': f'mig062-{value}-vault', 'r': value},
                )

            # A NULL role is also fine.
            await conn.execute(
                text('INSERT INTO vaults (id, name) VALUES (gen_random_uuid(), :n)'),
                {'n': 'mig062-null-vault'},
            )
            await conn.execute(
                text(
                    'INSERT INTO notes (id, vault_id, role) '
                    'VALUES (gen_random_uuid(), '
                    '  (SELECT id FROM vaults WHERE name = :n), NULL)'
                ),
                {'n': 'mig062-null-vault'},
            )

            # An unknown role value must be rejected.
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text('INSERT INTO vaults (id, name) VALUES (gen_random_uuid(), :n)'),
                    {'n': 'mig062-bad-vault'},
                )
                await conn.execute(
                    text(
                        'INSERT INTO notes (id, vault_id, role) '
                        'VALUES (gen_random_uuid(), '
                        '  (SELECT id FROM vaults WHERE name = :n), :r)'
                    ),
                    {'n': 'mig062-bad-vault', 'r': 'plan'},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_column_check_and_index(fresh_db_url: str) -> None:
    """Downgrade past 062 removes the column, the CHECK constraint, and the
    partial index."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert not await _column_exists(conn, 'notes', _COLUMN), (
                f'column {_COLUMN!r} should be gone after 062 downgrade'
            )
            assert not await _check_exists(conn, _CHECK), (
                f'CHECK {_CHECK!r} should be gone after 062 downgrade'
            )
            assert not await _index_exists(conn, _INDEX), (
                f'index {_INDEX!r} should be gone after 062 downgrade'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_round_trip(fresh_db_url: str) -> None:
    """upgrade → downgrade → upgrade leaves the schema in the expected state."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _column_exists(conn, 'notes', _COLUMN)
            assert await _check_exists(conn, _CHECK)
            assert await _index_exists(conn, _INDEX)
    finally:
        await engine.dispose()
