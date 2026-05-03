"""F22 — alembic 033_confidence_evidence_count migration tests.

Verifies the ``confidence_evidence_count`` column on ``memory_units`` and
the supporting ``idx_memory_links_link_type_to_unit`` composite index +
``memory_units_confidence_evidence_count_check`` CHECK constraint, plus
the upgrade → downgrade → upgrade round-trip on a real Postgres
container (Hermes round-13 LOW).

The unit tests under ``test_alembic_033_confidence_evidence_count.py``
exercise the chunked-backfill loop termination + SQL shape with mocks;
this file complements that with a live-DB run so the actual DDL is
exercised end-to-end.
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


_TARGET = '033_confidence_evidence_count'
_DOWN = '032_fsfm_decay_columns'
_COLUMN = 'confidence_evidence_count'
_INDEX = 'idx_memory_links_link_type_to_unit'
_CHECK = 'memory_units_confidence_evidence_count_check'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig033'):
        yield url


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_column_index_and_check(fresh_db_url: str) -> None:
    """Upgrade adds NOT NULL ``confidence_evidence_count`` (default 0), the
    composite index on ``memory_links(link_type, to_unit_id)``, and the
    ``confidence_evidence_count >= 0`` CHECK constraint."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col_row = (
                await conn.execute(
                    text(
                        'SELECT data_type, is_nullable, column_default '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).first()
            assert col_row is not None, f'column {_COLUMN!r} missing after upgrade'
            data_type, is_nullable, column_default = col_row
            assert data_type == 'integer', f'unexpected type {data_type!r}'
            assert is_nullable == 'NO', f'column should be NOT NULL, got {is_nullable!r}'
            assert column_default is not None and '0' in column_default, (
                f'column default should be 0, got {column_default!r}'
            )

            idx_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        'WHERE schemaname = current_schema() AND indexname = :name'
                    ),
                    {'name': _INDEX},
                )
            ).scalar()
            assert idx_exists == 1, f'expected index {_INDEX!r} after upgrade'

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
async def test_alembic_downgrade_drops_column_index_and_check(fresh_db_url: str) -> None:
    """Downgrade past 033 removes the column, index, and CHECK constraint."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar()
            assert col_exists is None, f'column {_COLUMN!r} should be gone after downgrade'

            idx_exists = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        'WHERE schemaname = current_schema() AND indexname = :name'
                    ),
                    {'name': _INDEX},
                )
            ).scalar()
            assert idx_exists is None, f'index {_INDEX!r} should be gone after downgrade'

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
async def test_alembic_round_trip_upgrade_downgrade_upgrade(fresh_db_url: str) -> None:
    """upgrade → downgrade → upgrade on a live DB.

    Hermes round-13 LOW: the inline backfill SQL was tested in
    ``test_int_f22_confidence_composition.py`` and the chunked-loop
    termination was tested with mocks in
    ``test_alembic_033_confidence_evidence_count.py``, but the full
    reversibility round-trip on a live database was untested. This test
    closes that gap so a future edit to ``033`` that breaks downgrade
    cannot land green.
    """
    # First upgrade: pristine DB.
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    # Verify column / index / CHECK present.
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() == 1
    finally:
        await engine.dispose()

    # Downgrade to 032.
    await _alembic_downgrade(fresh_db_url, _DOWN)

    # Verify the F22 artefacts are gone but the rest of the schema survives.
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() is None
            # memory_units itself must still be present — downgrade is
            # narrow (column + index + constraint), not destructive.
            assert (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = 'memory_units'"
                    )
                )
            ).scalar() == 1
    finally:
        await engine.dispose()

    # Re-upgrade. This is the key reversibility assertion: the second
    # upgrade must succeed against a DB whose 033 artefacts were just
    # dropped by ``downgrade()`` — not against a fully fresh schema.
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'memory_units' AND column_name = :col"
                    ),
                    {'col': _COLUMN},
                )
            ).scalar() == 1
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        'WHERE schemaname = current_schema() AND indexname = :name'
                    ),
                    {'name': _INDEX},
                )
            ).scalar() == 1
            assert (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.table_constraints '
                        "WHERE constraint_name = :name AND constraint_type = 'CHECK'"
                    ),
                    {'name': _CHECK},
                )
            ).scalar() == 1
    finally:
        await engine.dispose()
