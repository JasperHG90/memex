"""Integration tests for migration 027_consolidation_ticks (F38).

Verifies the vault-scoped ``consolidation_ticks`` summary table created by
the migration: column shape, FK to ``vaults.id`` with ON DELETE CASCADE,
both indexes (started_at history + completed_at MAX-lookup), and
upgrade/downgrade reversibility. Mirrors the structure of
``test_int_alembic_028.py``.
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

_STUBS_BETWEEN: list[str] = [
    # 025_maintenance_proposals (F6 #20) and 026_revisit_columns (F20 #24)
    # are both no longer stubs; the real migrations run cleanly so no
    # neutralization is required. Kept as an empty list so the
    # `with neutralized_stub_revisions(...)` blocks below remain
    # syntactically valid and ready if a future stub gets inserted.
]


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig027'):
        yield url


@pytest.mark.asyncio
async def test_creates_consolidation_ticks(fresh_db_url: str) -> None:
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='027_consolidation_ticks')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        'SELECT column_name, data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'consolidation_ticks' "
                        'ORDER BY ordinal_position'
                    )
                )
            ).all()
        assert cols, 'consolidation_ticks table missing after upgrade.'
        col_map = {row[0]: (row[1], row[2]) for row in cols}

        # Wave 0 invariant: vault_id NOT NULL.
        assert col_map['vault_id'][1] == 'NO'
        assert col_map['vault_id'][0] == 'uuid'

        # started_at NOT NULL; completed_at NULLABLE (NULL = in-progress).
        assert col_map['started_at'][1] == 'NO'
        assert col_map['completed_at'][1] == 'YES'

        # Counters NOT NULL with default 0.
        for c in ('units_processed', 'entities_reflected', 'contradictions_run', 'stale_pruned'):
            assert col_map[c][1] == 'NO', f'{c} must be NOT NULL'
            assert col_map[c][0] in ('integer', 'bigint')

        assert col_map['error'][1] == 'YES'
        assert col_map['created_at'][1] == 'NO'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_at_index_supports_max_lookup(fresh_db_url: str) -> None:
    """``idx_consolidation_ticks_vault_completed`` is the covering index for
    ``MAX(completed_at) WHERE vault_id = ?`` used by ``get_last_tick_timestamp``.
    """
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='027_consolidation_ticks')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE indexname = 'idx_consolidation_ticks_vault_completed' "
                        "AND tablename = 'consolidation_ticks'"
                    )
                )
            ).first()
        assert row is not None, (
            'Expected idx_consolidation_ticks_vault_completed for MAX(completed_at) lookup.'
        )
        defn = row[0].lower()
        assert 'vault_id' in defn
        assert 'completed_at desc nulls last' in defn, (
            f'Expected DESC NULLS LAST ordering on completed_at; got {row[0]!r}'
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fk_cascades_on_vault_delete(fresh_db_url: str) -> None:
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='027_consolidation_ticks')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT confdeltype
                        FROM pg_constraint
                        WHERE conrelid = 'consolidation_ticks'::regclass
                          AND contype = 'f'
                          AND confrelid = 'vaults'::regclass
                          AND conname = 'fk_consolidation_ticks_vault_id'
                        """
                    )
                )
            ).first()
        assert row is not None
        confdeltype = row[0]
        if isinstance(confdeltype, bytes):
            confdeltype = confdeltype.decode()
        assert confdeltype == 'c', f'Expected ON DELETE CASCADE; got {confdeltype!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_027_round_trip_clean(fresh_db_url: str) -> None:
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='027_consolidation_ticks')
        await _alembic_downgrade(fresh_db_url, '026_revisit_columns')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tbl = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.tables '
                        "WHERE table_name = 'consolidation_ticks'"
                    )
                )
            ).scalar()
            assert tbl is None
            for idx_name in (
                'idx_consolidation_ticks_vault_started',
                'idx_consolidation_ticks_vault_completed',
            ):
                idx = (
                    await conn.execute(
                        text('SELECT 1 FROM pg_indexes WHERE indexname = :n'),
                        {'n': idx_name},
                    )
                ).scalar()
                assert idx is None, f'{idx_name} should be gone after downgrade.'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cascade_clears_tick_rows(fresh_db_url: str) -> None:
    """Deleting a vault removes its consolidation_ticks rows."""
    with neutralized_stub_revisions(_STUBS_BETWEEN):
        await _alembic_upgrade(fresh_db_url, target='027_consolidation_ticks')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        vault_id = uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO vaults (id, name, description) VALUES (:id, :n, '')"),
                {'id': vault_id, 'n': f'v-{vault_id.hex[:8]}'},
            )
            await conn.execute(
                text(
                    'INSERT INTO consolidation_ticks (vault_id, started_at, completed_at) '
                    'VALUES (:vid, now(), now())'
                ),
                {'vid': vault_id},
            )

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text('SELECT COUNT(*) FROM consolidation_ticks WHERE vault_id = :v'),
                    {'v': vault_id},
                )
            ).scalar()
            assert count == 1

        async with engine.begin() as conn:
            await conn.execute(text('DELETE FROM vaults WHERE id = :v'), {'v': vault_id})

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text('SELECT COUNT(*) FROM consolidation_ticks WHERE vault_id = :v'),
                    {'v': vault_id},
                )
            ).scalar()
            assert count == 0
    finally:
        await engine.dispose()
