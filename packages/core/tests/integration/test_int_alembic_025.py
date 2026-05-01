"""F6 — alembic 025_maintenance_proposals migration tests (TC-20-1).

Verifies the `maintenance_proposals` table and its three indexes are created,
that the partial unique index actually enforces idempotency only on `pending`
rows, and that the upgrade/downgrade pair is reversible.
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

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


_TARGET = '025_maintenance_proposals'
_DOWN = '024_intent_risk_classifier'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig025'):
        yield url


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_table_and_indexes(fresh_db_url: str) -> None:
    """TC-20-1a: upgrade creates `maintenance_proposals` with the 3 expected indexes."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        'SELECT column_name, data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'maintenance_proposals' "
                        'ORDER BY ordinal_position'
                    )
                )
            ).all()
            col_map = {row[0]: (row[1], row[2]) for row in cols}

            assert 'id' in col_map and col_map['id'][0] == 'uuid'
            assert 'vault_id' in col_map and col_map['vault_id'][1] == 'YES', (
                'vault_id MUST be nullable per AC-F6-1 (NULL = global findings)'
            )
            assert 'lint_type' in col_map and col_map['lint_type'][1] == 'NO'
            assert 'target_type' in col_map and col_map['target_type'][1] == 'NO'
            assert 'target_id' in col_map and col_map['target_id'][1] == 'NO'
            assert 'rule_name' in col_map and col_map['rule_name'][1] == 'NO'
            assert 'evidence' in col_map and col_map['evidence'][0] == 'jsonb'
            assert 'suggested_action' in col_map and col_map['suggested_action'][1] == 'NO'
            assert 'status' in col_map and col_map['status'][1] == 'NO'
            assert 'source' in col_map and col_map['source'][1] == 'NO'
            # created_at + resolved_at: introspection reports YES across the
            # codebase for TIMESTAMP columns with server_default=now() (see
            # note_appends.applied_at for the same pattern). Defaults populate
            # the value at insert time even with nullable=YES.
            assert 'created_at' in col_map and col_map['created_at'][0].startswith('timestamp')
            assert 'resolved_at' in col_map and col_map['resolved_at'][0].startswith('timestamp')

            indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE tablename = 'maintenance_proposals'"
                        )
                    )
                ).all()
            }
            assert 'uq_maintenance_proposals_pending' in indexes
            assert 'idx_maintenance_proposals_vault_status' in indexes
            assert 'idx_maintenance_proposals_lint_type' in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_downgrade_drops_table(fresh_db_url: str) -> None:
    """TC-20-1b: downgrade past 025 leaves the table and all 3 indexes gone."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tbl = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM information_schema.tables '
                        "WHERE table_name = 'maintenance_proposals'"
                    )
                )
            ).scalar()
            assert tbl is None

            for ix in (
                'uq_maintenance_proposals_pending',
                'idx_maintenance_proposals_vault_status',
                'idx_maintenance_proposals_lint_type',
            ):
                exists = (
                    await conn.execute(
                        text('SELECT 1 FROM pg_indexes WHERE indexname = :ix'),
                        {'ix': ix},
                    )
                ).scalar()
                assert exists is None, f'index {ix} should be gone after downgrade'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_pending_dup(fresh_db_url: str) -> None:
    """TC-20-1c: partial unique index allows duplicates only when at least one is non-pending."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await conn.commit()

        async with engine.connect() as conn:
            vault_id = uuid4()
            await conn.execute(
                text(
                    'INSERT INTO vaults (id, name, description) '
                    "VALUES (:id, :name, 'F6 partial-unique-index test')"
                ),
                {'id': vault_id, 'name': f'mig025_{uuid4().hex[:8]}'},
            )

            target_id = str(uuid4())
            params = {
                'vault_id': vault_id,
                'target_id': target_id,
            }

            await conn.execute(
                text(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, '
                    ' evidence, suggested_action) '
                    "VALUES (:vault_id, 'quality', 'memory_unit', :target_id, "
                    " 'cold_low_mw_unit', '{}'::jsonb, 'fix it')"
                ),
                params,
            )
            await conn.commit()

            # Second pending insert with identical (rule_name, target_type, target_id, vault_id)
            # must fail.
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        'INSERT INTO maintenance_proposals '
                        '(vault_id, lint_type, target_type, target_id, rule_name, '
                        ' evidence, suggested_action) '
                        "VALUES (:vault_id, 'quality', 'memory_unit', :target_id, "
                        " 'cold_low_mw_unit', '{}'::jsonb, 'fix it again')"
                    ),
                    params,
                )
            await conn.rollback()

            # Flip the existing row to resolved; second pending insert should now succeed
            # (partial index ignores resolved rows).
            await conn.execute(
                text(
                    "UPDATE maintenance_proposals SET status = 'resolved', resolved_at = now() "
                    "WHERE rule_name = 'cold_low_mw_unit' "
                    '  AND target_id = :target_id AND vault_id = :vault_id'
                ),
                params,
            )
            await conn.execute(
                text(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, '
                    ' evidence, suggested_action) '
                    "VALUES (:vault_id, 'quality', 'memory_unit', :target_id, "
                    " 'cold_low_mw_unit', '{}'::jsonb, 'pending again')"
                ),
                params,
            )
            await conn.commit()

            count = (
                await conn.execute(
                    text(
                        'SELECT count(*) FROM maintenance_proposals '
                        "WHERE rule_name = 'cold_low_mw_unit' "
                        '  AND target_id = :target_id AND vault_id = :vault_id'
                    ),
                    params,
                )
            ).scalar()
            assert count == 2, (
                'Expected 2 rows after resolved+pending sequence; partial index should not block.'
            )
    finally:
        await engine.dispose()
