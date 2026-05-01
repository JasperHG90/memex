"""F20 — alembic 026_revisit_columns migration tests (TC-24-1).

Verifies the four `revisit_*` columns + the partial index are created on
`memory_units`, that the partial index uses the `revisit_due_at IS NOT NULL`
predicate, and that the upgrade/downgrade pair is reversible.
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


_TARGET = '026_revisit_columns'
_DOWN = '025_maintenance_proposals'

_REVISIT_COLUMNS = (
    ('revisit_due_at', 'timestamp', 'YES'),  # nullable
    ('revisit_stability', 'double precision', 'YES'),
    ('revisit_difficulty', 'double precision', 'YES'),
    ('revisit_review_count', 'integer', 'NO'),  # NOT NULL DEFAULT 0
)
_PARTIAL_INDEX = 'idx_memory_units_revisit_due_at'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig026'):
        yield url


@pytest.mark.asyncio
async def test_alembic_upgrade_adds_revisit_columns_and_partial_index(
    fresh_db_url: str,
) -> None:
    """TC-24-1a: upgrade adds the 4 revisit columns + partial index on memory_units."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        'SELECT column_name, data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' "
                        "AND column_name LIKE 'revisit_%' "
                        'ORDER BY column_name'
                    )
                )
            ).all()
            col_map = {row[0]: (row[1], row[2]) for row in cols}

            for name, expected_type, expected_nullable in _REVISIT_COLUMNS:
                assert name in col_map, f'column {name!r} missing'
                actual_type, actual_nullable = col_map[name]
                assert actual_type.startswith(expected_type), (
                    f'{name}: type {actual_type!r} does not start with {expected_type!r}'
                )
                assert actual_nullable == expected_nullable, (
                    f'{name}: nullable={actual_nullable!r}, expected {expected_nullable!r}'
                )

            # Partial index exists with the correct predicate
            row = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE tablename = 'memory_units' AND indexname = :ix"
                    ),
                    {'ix': _PARTIAL_INDEX},
                )
            ).scalar()
            assert row is not None, f'partial index {_PARTIAL_INDEX!r} missing'
            assert 'revisit_due_at IS NOT NULL' in row, f'partial-index predicate missing: {row!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_downgrade_drops_revisit_columns_and_index(
    fresh_db_url: str,
) -> None:
    """TC-24-1b: downgrade past 026 removes all 4 columns + partial index."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            for name, _t, _n in _REVISIT_COLUMNS:
                exists = (
                    await conn.execute(
                        text(
                            'SELECT 1 FROM information_schema.columns '
                            "WHERE table_name = 'memory_units' AND column_name = :n"
                        ),
                        {'n': name},
                    )
                ).scalar()
                assert exists is None, f'column {name!r} should be gone after downgrade'

            ix_exists = (
                await conn.execute(
                    text('SELECT 1 FROM pg_indexes WHERE indexname = :ix'),
                    {'ix': _PARTIAL_INDEX},
                )
            ).scalar()
            assert ix_exists is None, f'index {_PARTIAL_INDEX!r} should be gone after downgrade'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_revisit_review_count_default_is_zero(fresh_db_url: str) -> None:
    """TC-24-1c: server_default='0' applies on INSERT — never-reviewed rows start at 0,
    not NULL. Confirms the streak check `WHERE revisit_review_count >= 5` has no NULL
    edge case (per the team-lead's nullability nit).
    """
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT column_default FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' "
                        "AND column_name = 'revisit_review_count'"
                    )
                )
            ).scalar()
            assert row is not None, 'revisit_review_count must have a server_default'
            assert '0' in row, f'server_default should be 0, got {row!r}'
    finally:
        await engine.dispose()
