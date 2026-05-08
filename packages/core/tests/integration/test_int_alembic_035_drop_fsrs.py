"""Migration round-trip test for the FSRS-revisit column drop (035).

Verifies that:
- Upgrading to ``035_drop_fsrs_revisit_columns`` removes the five
  ``revisit_*`` columns added by 026 and 030 plus the partial index
  ``idx_memory_units_revisit_due_at``.
- Downgrading from 035 back to 034 restores all five columns and the
  index in their original shape (rollback parity).
- The unrelated FSFM-decay columns added by 032 (``importance``,
  ``stability``, ``last_outcome_at``) survive the round-trip — these
  are NOT FSRS and the new scorer keeps using them.
- A row inserted with a populated ``revisit_review_count`` before the
  upgrade still exists after the upgrade (only the columns are dropped,
  not the rows).
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
)

pytestmark = [pytest.mark.integration]


_TARGET_BEFORE = '034_add_mw_mode'
_TARGET_AFTER = '035_drop_fsrs_revisit_columns'

_DROPPED_COLUMNS = (
    'revisit_due_at',
    'revisit_stability',
    'revisit_difficulty',
    'revisit_review_count',
    'revisit_last_reviewed_at',
)

_PARTIAL_INDEX = 'idx_memory_units_revisit_due_at'

# Columns that must survive the upgrade — they're FSFM-decay (032), not FSRS.
_PRESERVED_COLUMNS = ('importance', 'stability', 'last_outcome_at')


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig035'):
        yield url


async def _columns_present(conn, columns: tuple[str, ...]) -> dict[str, bool]:
    rows = (
        await conn.execute(
            text(
                'SELECT column_name FROM information_schema.columns '
                "WHERE table_name = 'memory_units' AND column_name = ANY(:cols)"
            ),
            {'cols': list(columns)},
        )
    ).all()
    found = {row[0] for row in rows}
    return {c: c in found for c in columns}


async def _index_present(conn, index_name: str) -> bool:
    return (
        await conn.execute(
            text('SELECT 1 FROM pg_indexes WHERE indexname = :ix'),
            {'ix': index_name},
        )
    ).scalar() is not None


@pytest.mark.asyncio
async def test_upgrade_drops_revisit_columns_and_partial_index(fresh_db_url: str) -> None:
    """Upgrade through 035 → all five revisit columns and the partial index gone."""
    # Pre-state: stop at 034 so revisit columns are still present.
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            before = await _columns_present(conn, _DROPPED_COLUMNS)
            assert all(before.values()), f'precondition: revisit cols missing pre-upgrade: {before}'
            assert await _index_present(conn, _PARTIAL_INDEX), (
                'precondition: revisit partial index missing pre-upgrade'
            )
    finally:
        await engine.dispose()

    # Apply the new migration.
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            after = await _columns_present(conn, _DROPPED_COLUMNS)
            assert not any(after.values()), f'revisit cols still present post-upgrade: {after}'
            assert not await _index_present(conn, _PARTIAL_INDEX), (
                'revisit partial index still present post-upgrade'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_restores_revisit_columns_and_index(fresh_db_url: str) -> None:
    """Downgrade past 035 restores columns and partial index for rollback parity."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            restored = await _columns_present(conn, _DROPPED_COLUMNS)
            assert all(restored.values()), f'revisit cols not restored after downgrade: {restored}'
            assert await _index_present(conn, _PARTIAL_INDEX), (
                'revisit partial index not restored after downgrade'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fsfm_decay_columns_survive_upgrade(fresh_db_url: str) -> None:
    """Migration 032's FSFM-decay columns must not be touched by 035.

    The new graph-aware scorer keeps reading ``importance``, ``stability``,
    and ``last_outcome_at`` directly. Dropping them here would silently
    break the scorer's temporal-staleness component.
    """
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            preserved = await _columns_present(conn, _PRESERVED_COLUMNS)
            assert all(preserved.values()), (
                f'FSFM-decay columns lost during 035 upgrade: {preserved}'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_unit_rows_preserved_through_upgrade(fresh_db_url: str) -> None:
    """Rows in memory_units survive — only the columns are dropped.

    Uses raw SQL keyed against the actual schema (notes.original_text,
    memory_units.text). The point of this test is structural — that
    DROP COLUMN does not delete rows — so we seed only the minimum
    columns needed to satisfy the FKs and NOT-NULLs at revision 034.
    """
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)

    vault_id = uuid4()
    note_id = uuid4()
    unit_id = uuid4()
    embedding = '[' + ','.join(['0.1'] * 384) + ']'

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
                {'id': vault_id, 'name': f'mig035-{uuid4().hex[:8]}'},
            )
            await conn.execute(
                text(
                    'INSERT INTO notes (id, vault_id, title, original_text) '
                    "VALUES (:id, :vault_id, 'mig035 note', 'seed text')"
                ),
                {'id': note_id, 'vault_id': vault_id},
            )
            # Seed a memory_unit with revisit_review_count populated so we
            # confirm the row survives even though the column will be dropped.
            await conn.execute(
                text(
                    'INSERT INTO memory_units '
                    '(id, vault_id, note_id, text, fact_type, status, '
                    'embedding, event_date, intent_class, risk_class, '
                    'confidence, confidence_evidence_count, is_deprioritized, '
                    'success_co_count, failure_co_count, revisit_review_count) '
                    "VALUES (:id, :vault_id, :note_id, 'mig-035 marker', "
                    "'world', 'active', CAST(:emb AS vector), now(), "
                    "'durable', 'none', 1.0, 0, false, 0, 0, 4)"
                ),
                {
                    'id': unit_id,
                    'vault_id': vault_id,
                    'note_id': note_id,
                    'emb': embedding,
                },
            )
    finally:
        await engine.dispose()

    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT id, text FROM memory_units WHERE id = :id'),
                    {'id': unit_id},
                )
            ).first()
            assert row is not None, 'unit row was lost during column-drop migration'
            assert row[1] == 'mig-035 marker'
    finally:
        await engine.dispose()
