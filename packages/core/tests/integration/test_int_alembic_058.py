"""Migration round-trip test for the vault-summary embedding column (058).

Verifies via a real ``alembic upgrade`` / ``downgrade`` (not create_all),
against a populated table, that:
- 058 adds a nullable ``embedding`` column to ``vault_summaries``.
- A pre-existing row (seeded at 057) SURVIVES the upgrade with its data
  intact and ``embedding IS NULL`` (lazy backfill — the column is not
  populated by the migration).
- Downgrade drops the column and leaves the row's other data intact.
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

_TARGET_BEFORE = '057_lint_source_external'
_TARGET_AFTER = '058_vault_summary_embedding'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig058'):
        yield url


async def _seed_summary_at_057(db_url: str) -> str:
    """Upgrade to 057 and seed one vault + one vault_summaries row. Returns vault_id."""
    await _alembic_upgrade(db_url, target=_TARGET_BEFORE)
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            vid = (
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name) '
                        "VALUES (gen_random_uuid(), 'emb-mig-058') RETURNING id"
                    )
                )
            ).scalar()
            await conn.execute(
                text(
                    'INSERT INTO vault_summaries '
                    '(id, vault_id, narrative, themes, inventory, key_entities, '
                    ' version, notes_incorporated, patch_log) '
                    "VALUES (gen_random_uuid(), :v, 'pre-existing narrative', "
                    "'[]'::jsonb, '{}'::jsonb, '[]'::jsonb, 3, 7, '[]'::jsonb)"
                ),
                {'v': vid},
            )
        return str(vid)
    finally:
        await engine.dispose()


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


@pytest.mark.asyncio
async def test_upgrade_adds_nullable_embedding_and_preserves_rows(fresh_db_url: str) -> None:
    vid = await _seed_summary_at_057(fresh_db_url)

    # 057 has no embedding column.
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert not await _column_exists(conn, 'vault_summaries', 'embedding')
    finally:
        await engine.dispose()

    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _column_exists(conn, 'vault_summaries', 'embedding')
            row = (
                await conn.execute(
                    text(
                        'SELECT narrative, version, notes_incorporated, embedding '
                        'FROM vault_summaries WHERE vault_id = :v'
                    ),
                    {'v': vid},
                )
            ).one()
            # Pre-existing data intact; embedding lazily NULL (not backfilled).
            assert row[0] == 'pre-existing narrative'
            assert row[1] == 3
            assert row[2] == 7
            assert row[3] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_column_and_keeps_row(fresh_db_url: str) -> None:
    vid = await _seed_summary_at_057(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert not await _column_exists(conn, 'vault_summaries', 'embedding')
            narrative = (
                await conn.execute(
                    text('SELECT narrative FROM vault_summaries WHERE vault_id = :v'),
                    {'v': vid},
                )
            ).scalar()
            assert narrative == 'pre-existing narrative'
    finally:
        await engine.dispose()
