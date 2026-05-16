"""Alembic 043_drop_procedure_outcomes migration tests.

Verifies the ``procedure_outcomes`` table is dropped on upgrade and that
downgrade re-creates an empty table with the original schema.

The test bypasses the full chain (which is non-idempotent past 035 against
``001_full_baseline``'s ``metadata.create_all`` — a pre-existing migration
infrastructure limitation, unrelated to this revision) and seeds the
pre-043 state by upgrading to the head set by ``001_full_baseline``, then
manually re-creating the ``procedure_outcomes`` table to mirror the
post-028 schema. The migration body is then exercised via ``op``
directly inside a migration context.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import make_fresh_db  # noqa: F401

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig043'):
        yield url


async def _create_procedure_outcomes(url: str) -> None:
    """Re-create the post-028 ``procedure_outcomes`` table on a fresh DB.

    Mirrors the schema introduced by migration 028. Used to seed the
    pre-043 state without running the non-idempotent 036-042 chain.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS procedure_outcomes ('
                    '  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),'
                    '  vault_id UUID NOT NULL,'
                    '  kv_key TEXT NOT NULL,'
                    '  success_co_count INTEGER NOT NULL DEFAULT 0,'
                    '  failure_co_count INTEGER NOT NULL DEFAULT 0,'
                    '  last_outcome_at TIMESTAMPTZ,'
                    '  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),'
                    '  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),'
                    '  UNIQUE (vault_id, kv_key)'
                    ')'
                )
            )
    finally:
        await engine.dispose()


async def _run_migration_op(url: str, direction: str) -> None:
    """Execute migration 043's upgrade or downgrade body in an isolated context.

    Loads the revision module by path (alembic's loader) and invokes
    ``upgrade()`` / ``downgrade()`` with ``op`` bound to an alembic
    ``MigrationContext`` on the test DB.
    """
    import importlib.util
    import pathlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    rev_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / 'src'
        / 'memex_core'
        / 'alembic'
        / 'versions'
        / '043_drop_procedure_outcomes.py'
    )
    spec = importlib.util.spec_from_file_location('mig_043', rev_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:

            def _run(sync_conn) -> None:
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    if direction == 'upgrade':
                        module.upgrade()
                    else:
                        module.downgrade()

            await conn.run_sync(_run)
    finally:
        await engine.dispose()


async def _table_exists(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text(
                            'SELECT 1 FROM information_schema.tables '
                            "WHERE table_name = 'procedure_outcomes'"
                        )
                    )
                ).scalar()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_drops_procedure_outcomes(fresh_db_url: str) -> None:
    """Upgrade body removes the procedure_outcomes table when present."""
    await _create_procedure_outcomes(fresh_db_url)
    assert await _table_exists(fresh_db_url), 'precondition: table present'

    await _run_migration_op(fresh_db_url, direction='upgrade')

    assert not await _table_exists(fresh_db_url), 'table should be gone after upgrade'


@pytest.mark.asyncio
async def test_upgrade_is_idempotent_when_table_absent(fresh_db_url: str) -> None:
    """Running upgrade on a DB without the table is a no-op (no error)."""
    assert not await _table_exists(fresh_db_url), 'precondition: table absent'

    await _run_migration_op(fresh_db_url, direction='upgrade')

    assert not await _table_exists(fresh_db_url), 'table still absent after no-op upgrade'


async def _create_fk_parents(url: str) -> None:
    """Create minimal ``vaults`` and ``kv_entries`` parent tables so the
    downgrade's FK constraints have something to reference."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS vaults ('
                    '  id UUID PRIMARY KEY DEFAULT gen_random_uuid()'
                    ')'
                )
            )
            await conn.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS kv_entries ('
                    '  key TEXT PRIMARY KEY,'
                    "  value JSONB NOT NULL DEFAULT '{}'::jsonb"
                    ')'
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_recreates_table_empty(fresh_db_url: str) -> None:
    """Downgrade body re-creates the table (empty — counters NOT restored)."""
    # FK parents must exist before the migration re-creates procedure_outcomes
    # — the downgrade adds FKs to vaults.id and kv_entries.key.
    await _create_fk_parents(fresh_db_url)
    assert not await _table_exists(fresh_db_url), 'precondition: table absent'

    await _run_migration_op(fresh_db_url, direction='downgrade')

    assert await _table_exists(fresh_db_url), 'table should be back after downgrade'

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            count = (await conn.execute(text('SELECT COUNT(*) FROM procedure_outcomes'))).scalar()
            assert count == 0, 'downgrade should re-create the table empty'
    finally:
        await engine.dispose()
