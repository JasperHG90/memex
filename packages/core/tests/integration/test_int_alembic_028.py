"""Integration tests for migration 028_procedure_outcomes (F14).

Verifies the vault-scoped ``procedure_outcomes`` MW counter table created by
the migration: column shape, unique constraint on ``(vault_id, kv_key)``, FK
to ``kv_entries.key`` with ``ON DELETE CASCADE``, secondary index, and
upgrade/downgrade reversibility.

Reuses the helpers from ``_alembic_test_helpers.py``. Because 028 sits
downstream of three still-stub revisions (025/F6, 026/F20, 027/F38), the
tests neutralize those stubs in-process via ``neutralized_stub_revisions``
so this test only exercises the F14 DDL.
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import (  # noqa: F401
    alembic_downgrade as _alembic_downgrade,
    alembic_upgrade as _alembic_upgrade,
    make_fresh_db,
    neutralized_stub_revisions,
)

pytestmark = [pytest.mark.integration]

_STUBS_BETWEEN = [
    '025_maintenance_proposals',
    '026_revisit_columns',
    '027_consolidation_ticks',
]


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    """Create an empty DB in the session container, yield its URL, then drop it."""
    async for url in make_fresh_db(postgres_container, db_prefix='mig028'):
        yield url


@pytest.mark.asyncio
async def test_procedure_outcomes_table_present_with_expected_shape(fresh_db_url: str) -> None:
    """After upgrade to 028, procedure_outcomes has the columns + types declared by the migration."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        'SELECT column_name, data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'procedure_outcomes' "
                        'ORDER BY ordinal_position'
                    )
                )
            ).all()
        assert cols, 'procedure_outcomes table missing after upgrade.'
        col_map = {row[0]: (row[1], row[2]) for row in cols}

        # vault-scoped (Wave 0 invariant): vault_id NOT NULL
        assert 'vault_id' in col_map
        assert col_map['vault_id'][0] == 'uuid'
        assert col_map['vault_id'][1] == 'NO', (
            'vault_id MUST be NOT NULL — Wave 0 vault-scoping invariant for '
            'all per-vault counter tables.'
        )

        assert 'kv_key' in col_map
        assert col_map['kv_key'][0] in ('text', 'character varying')
        assert col_map['kv_key'][1] == 'NO'

        assert 'success_co_count' in col_map
        assert col_map['success_co_count'][0] in ('integer', 'bigint')
        assert col_map['success_co_count'][1] == 'NO'

        assert 'failure_co_count' in col_map
        assert col_map['failure_co_count'][0] in ('integer', 'bigint')
        assert col_map['failure_co_count'][1] == 'NO'

        assert 'last_outcome_at' in col_map
        assert col_map['last_outcome_at'][0].startswith('timestamp')
        assert col_map['last_outcome_at'][1] == 'YES'

        assert 'created_at' in col_map
        assert col_map['created_at'][0].startswith('timestamp')
        assert col_map['created_at'][1] == 'NO'

        assert 'updated_at' in col_map
        assert col_map['updated_at'][0].startswith('timestamp')
        assert col_map['updated_at'][1] == 'NO'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_procedure_outcomes_unique_constraint_on_vault_kv_key(fresh_db_url: str) -> None:
    """``UniqueConstraint(vault_id, kv_key)`` enforces vault-scoped isolation."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT a.attname
                        FROM pg_constraint c
                        JOIN pg_attribute a
                          ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                        WHERE c.conrelid = 'procedure_outcomes'::regclass
                          AND c.conname = 'uq_procedure_outcomes_vault_key'
                        ORDER BY array_position(c.conkey, a.attnum)
                        """
                    )
                )
            ).all()
        cols = [r[0] for r in row]
        assert cols == ['vault_id', 'kv_key'], (
            f'Expected uq_procedure_outcomes_vault_key on (vault_id, kv_key); got {cols}'
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_procedure_outcomes_fk_cascades_on_kv_entry_delete(fresh_db_url: str) -> None:
    """FK ``kv_key → kv_entries.key`` is ON DELETE CASCADE."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT confdeltype
                        FROM pg_constraint
                        WHERE conrelid = 'procedure_outcomes'::regclass
                          AND contype = 'f'
                          AND confrelid = 'kv_entries'::regclass
                          AND conname = 'fk_procedure_outcomes_kv_key'
                        """
                    )
                )
            ).first()
        assert row is not None, (
            'Expected a foreign key from procedure_outcomes.kv_key → kv_entries.key.'
        )
        confdeltype = row[0]
        if isinstance(confdeltype, bytes):
            confdeltype = confdeltype.decode()
        assert confdeltype == 'c', (
            f'Expected ON DELETE CASCADE (confdeltype="c"), got {confdeltype!r}. '
            'Without cascade, deleting a procedure key would leave orphan counter rows.'
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_procedure_outcomes_secondary_index(fresh_db_url: str) -> None:
    """The unique constraint ``uq_procedure_outcomes_vault_key`` auto-creates a
    btree index covering (vault_id, kv_key); no separate ``idx_*`` is needed."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE indexname = 'uq_procedure_outcomes_vault_key' "
                        "AND tablename = 'procedure_outcomes'"
                    )
                )
            ).first()
        assert row is not None, (
            'Expected uq_procedure_outcomes_vault_key index '
            '(auto-created by the unique constraint) for per-vault counter lookups.'
        )
        defn = row[0].lower()
        assert 'vault_id' in defn and 'kv_key' in defn
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_028_round_trip_clean(fresh_db_url: str) -> None:
    """upgrade → downgrade past 028 leaves procedure_outcomes gone."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')
        await _alembic_downgrade(fresh_db_url, '027_consolidation_ticks')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tbl = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.tables '
                        "WHERE table_name = 'procedure_outcomes'"
                    )
                )
            ).scalar()
            assert tbl is None, 'procedure_outcomes should be gone after downgrade.'

            idx = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        "WHERE indexname = 'uq_procedure_outcomes_vault_key'"
                    )
                )
            ).scalar()
            assert idx is None, 'unique-constraint index should be gone after downgrade.'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fk_cascade_on_kv_entry_delete_clears_counter_row(fresh_db_url: str) -> None:
    """End-to-end FK cascade: deleting a kv_entries row removes its counter row."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='028_procedure_outcomes')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        kv_key = 'procedure:write_pr:commit-style'
        vault_id = uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO vaults (id, name, description) VALUES (:id, :name, '')"),
                {'id': vault_id, 'name': f'v-{vault_id.hex[:8]}'},
            )
            await conn.execute(
                text('INSERT INTO kv_entries (key, value) VALUES (:k, :v)'),
                {'k': kv_key, 'v': '{}'},
            )
            await conn.execute(
                text(
                    'INSERT INTO procedure_outcomes (vault_id, kv_key, '
                    'success_co_count, failure_co_count) '
                    'VALUES (:vid, :k, 1, 0)'
                ),
                {'vid': vault_id, 'k': kv_key},
            )

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text('SELECT COUNT(*) FROM procedure_outcomes WHERE kv_key = :k'),
                    {'k': kv_key},
                )
            ).scalar()
            assert count == 1

        async with engine.begin() as conn:
            await conn.execute(
                text('DELETE FROM kv_entries WHERE key = :k'),
                {'k': kv_key},
            )

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text('SELECT COUNT(*) FROM procedure_outcomes WHERE kv_key = :k'),
                    {'k': kv_key},
                )
            ).scalar()
            assert count == 0, (
                'FK ON DELETE CASCADE should have removed the counter row '
                'when the parent kv_entries row was deleted.'
            )
    finally:
        await engine.dispose()
