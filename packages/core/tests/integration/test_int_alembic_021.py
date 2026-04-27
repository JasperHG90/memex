"""Integration tests for migration 021_batch_jobs_input_note_keys.

These tests run `alembic upgrade head` against a real Postgres testcontainer
(`pgvector/pgvector:pg18-trixie`) and assert the schema landed as documented
in RFC-002 § "Schema (AC-019)":

- the column is JSONB, NOT NULL, default `[]`;
- a GIN index on the column uses the `jsonb_path_ops` opclass (not the default
  `jsonb_ops`) — required by AC-019 (b);
- the migration is reversible (round-trip via `alembic downgrade -1`);
- existing rows back-fill atomically with `[]` after upgrade — AC-019 (back-fill).

The integration `conftest.py`-defined `engine` fixture stamps `alembic_version`
at head and uses `SQLModel.metadata.create_all` rather than running migrations
in sequence. To verify the migration *itself*, these tests connect their own
async engine to the `postgres_uri` of the session-scoped container, run
alembic from a clean state, and inspect the schema.

To avoid mutating the session-scoped `engine`'s schema (other tests use it),
each test creates and drops its own database in the same Postgres instance.
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

# Mark the whole module as integration so it only runs under `-m integration`.
pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    """Create an empty database in the session container, yield its URL, then drop it."""
    async for url in make_fresh_db(postgres_container, db_prefix='mig021'):
        yield url


@pytest.mark.asyncio
async def test_input_note_keys_column_present_and_typed(fresh_db_url: str) -> None:
    """AC-019 (a): after `alembic upgrade head`, the column exists, is JSONB,
    NOT NULL, and defaults to a literal `[]`-shaped JSONB."""
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT data_type, is_nullable, column_default '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'batch_jobs' AND column_name = 'input_note_keys'"
                    )
                )
            ).first()

        assert row is not None, "Column 'input_note_keys' missing from batch_jobs after upgrade."
        data_type, is_nullable, column_default = row

        assert data_type == 'jsonb', f'Expected JSONB column, got {data_type!r}.'
        assert is_nullable == 'NO', 'Expected NOT NULL, got nullable column.'
        assert column_default is not None, 'Expected a literal default to be present.'
        # Postgres reports the literal default with the cast in canonical form.
        assert "'[]'" in column_default and 'jsonb' in column_default.lower(), (
            f"Expected default to contain literal '[]' and JSONB cast; got {column_default!r}."
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_input_note_keys_gin_index_with_jsonb_path_ops(fresh_db_url: str) -> None:
    """AC-019 (b): the GIN index uses the `jsonb_path_ops` opclass.

    The check joins `pg_index` (which records the opclass OID per index column)
    with `pg_opclass` to recover the human-readable opclass name. The default
    opclass for JSONB is `jsonb_ops`; PR3 needs the smaller/faster
    `jsonb_path_ops` because the only access pattern is `@>` containment.
    """
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            # Sanity: the named index exists on the right table with GIN access.
            existence = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE indexname = 'idx_batch_jobs_input_note_keys' "
                        "AND tablename = 'batch_jobs'"
                    )
                )
            ).first()
            assert existence is not None, (
                "Index 'idx_batch_jobs_input_note_keys' missing on batch_jobs."
            )
            indexdef = existence[0]
            assert 'using gin' in indexdef.lower(), f'Expected GIN index, got {indexdef!r}.'

            # Verify the opclass via the catalog (more reliable than parsing indexdef).
            opclass_row = (
                await conn.execute(
                    text(
                        """
                        SELECT op.opcname
                        FROM pg_index ix
                        JOIN pg_class i ON i.oid = ix.indexrelid
                        JOIN pg_opclass op ON op.oid = ix.indclass[0]
                        WHERE i.relname = 'idx_batch_jobs_input_note_keys'
                        """
                    )
                )
            ).first()
            assert opclass_row is not None, 'Could not resolve opclass for the new index.'
            assert opclass_row[0] == 'jsonb_path_ops', (
                f"Expected opclass 'jsonb_path_ops', got {opclass_row[0]!r}. "
                'jsonb_path_ops is required for AC-019 (b) — it is the smaller and '
                'faster opclass for the @> containment queries this PR depends on.'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_021_migration_round_trip(fresh_db_url: str) -> None:
    """AC-019 round-trip: ``alembic upgrade head`` then downgrade past 021
    leaves the column and index removed.

    Targets the explicit prior revision rather than ``-1`` so additional
    migrations layered on top of 021 don't change the meaning of this test.
    """
    await _alembic_upgrade(fresh_db_url)
    await _alembic_downgrade(fresh_db_url, '020_temporal_cooccurrences')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            col = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.columns '
                        "WHERE table_name = 'batch_jobs' AND column_name = 'input_note_keys'"
                    )
                )
            ).scalar()
            assert col is None, 'Column should be gone after downgrade -1.'

            idx = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        "WHERE indexname = 'idx_batch_jobs_input_note_keys'"
                    )
                )
            ).scalar()
            assert idx is None, 'Index should be gone after downgrade -1.'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_rows_have_empty_keys_after_upgrade(fresh_db_url: str) -> None:
    """AC-019 back-fill: rows that exist *before* the upgrade have `input_note_keys
    = []` afterward. The literal default keeps this metadata-only on PG ≥ 11.

    We migrate to the previous head (revision `020_temporal_cooccurrences`),
    insert a `batch_jobs` row, then run upgrade head to `021` and verify the
    pre-existing row reports `[]`.
    """
    from uuid import uuid4

    # Migrate to the revision *before* 021.
    await _alembic_upgrade(fresh_db_url, target='020_temporal_cooccurrences')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    pre_id = uuid4()
    pre_vault = uuid4()
    try:
        async with engine.begin() as conn:
            # Seed a vault row so the FK on batch_jobs.vault_id has something to point at.
            await conn.execute(
                text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
                {'id': str(pre_vault), 'name': 'mig021-pre'},
            )
            # Insert a batch_jobs row at schema version 020 (no input_note_keys yet).
            await conn.execute(
                text(
                    'INSERT INTO batch_jobs (id, vault_id, status, notes_count) '
                    "VALUES (:id, :vault_id, 'pending', 0)"
                ),
                {'id': str(pre_id), 'vault_id': str(pre_vault)},
            )
    finally:
        await engine.dispose()

    # Now upgrade to head (i.e., apply 021).
    await _alembic_upgrade(fresh_db_url, target='head')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT input_note_keys FROM batch_jobs WHERE id = :id'),
                    {'id': str(pre_id)},
                )
            ).first()
            assert row is not None, 'Pre-existing row should still be present after upgrade.'
            keys = row[0]
            # asyncpg / SQLAlchemy decode JSONB to a Python list.
            assert keys == [], (
                f'Pre-existing row should back-fill input_note_keys to [] (literal default). '
                f'Got {keys!r}.'
            )
    finally:
        await engine.dispose()
