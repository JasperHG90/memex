"""Integration tests for migration 022_note_appends (issue #56).

Verifies that the audit table created by the migration matches the SQLModel
declaration in `memex_core.memory.sql_models.NoteAppend` and that the
upgrade/downgrade pair is reversible. Reuses the helpers from
`test_int_alembic_021.py` and a sibling `fresh_db_url` fixture.
"""

from __future__ import annotations

import pathlib as plb
import secrets
from typing import AsyncGenerator
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]


def _alembic_cfg_for(db_url: str) -> Config:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    cfg = Config(str(package_dir / 'alembic.ini'))
    cfg.set_main_option('script_location', str(package_dir / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', db_url)
    return cfg


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    db_name = 'mig022_' + secrets.token_hex(6)

    base_url = postgres_container.get_connection_url().replace('psycopg2', 'asyncpg')
    parsed = urlparse(base_url)
    admin_url = urlunparse(parsed._replace(path='/postgres'))
    new_url = urlunparse(parsed._replace(path=f'/{db_name}'))

    admin_engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level='AUTOCOMMIT')
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()

    new_engine = create_async_engine(new_url, poolclass=NullPool)
    async with new_engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
    await new_engine.dispose()

    try:
        yield new_url
    finally:
        admin_engine = create_async_engine(
            admin_url, poolclass=NullPool, isolation_level='AUTOCOMMIT'
        )
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = :db AND pid <> pg_backend_pid()'
                ),
                {'db': db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


async def _alembic_upgrade(db_url: str, target: str = 'head') -> None:
    import asyncio

    cfg = _alembic_cfg_for(db_url)
    await asyncio.to_thread(command.upgrade, cfg, target)


async def _alembic_downgrade(db_url: str, target: str) -> None:
    import asyncio

    cfg = _alembic_cfg_for(db_url)
    await asyncio.to_thread(command.downgrade, cfg, target)


@pytest.mark.asyncio
async def test_note_appends_table_present_with_expected_shape(fresh_db_url: str) -> None:
    """After upgrade head, note_appends has the columns + types declared by NoteAppend."""
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        'SELECT column_name, data_type, is_nullable '
                        'FROM information_schema.columns '
                        "WHERE table_name = 'note_appends' "
                        'ORDER BY ordinal_position'
                    )
                )
            ).all()
        assert cols, 'note_appends table missing after upgrade.'
        col_map = {row[0]: (row[1], row[2]) for row in cols}

        assert 'append_id' in col_map
        assert col_map['append_id'][0] == 'uuid'
        assert col_map['append_id'][1] == 'NO'

        assert 'note_id' in col_map
        assert col_map['note_id'][0] == 'uuid'
        assert col_map['note_id'][1] == 'NO'

        assert 'delta_sha256' in col_map
        assert col_map['delta_sha256'][0] in ('text', 'character varying')
        assert col_map['delta_sha256'][1] == 'NO'

        assert 'delta_bytes' in col_map
        assert col_map['delta_bytes'][0] in ('integer', 'bigint')
        assert col_map['delta_bytes'][1] == 'NO'

        assert 'joiner' in col_map
        assert col_map['joiner'][1] == 'NO'

        assert 'resulting_content_hash' in col_map
        assert col_map['resulting_content_hash'][1] == 'NO'

        assert 'new_unit_ids' in col_map
        assert col_map['new_unit_ids'][0] == 'ARRAY'

        assert 'applied_at' in col_map
        assert col_map['applied_at'][0].startswith('timestamp')
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_appends_primary_key_is_append_id(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT a.attname
                        FROM pg_index i
                        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                        WHERE i.indrelid = 'note_appends'::regclass AND i.indisprimary
                        """
                    )
                )
            ).all()
        pk_cols = {r[0] for r in row}
        assert pk_cols == {'append_id'}, f'Expected PK to be append_id only, got {pk_cols}.'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_appends_foreign_key_cascades_on_note_delete(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT confdeltype
                        FROM pg_constraint
                        WHERE conrelid = 'note_appends'::regclass
                          AND contype = 'f'
                          AND confrelid = 'notes'::regclass
                        """
                    )
                )
            ).first()
        assert row is not None, 'Expected a foreign key from note_appends.note_id → notes.id.'
        confdeltype = row[0]
        if isinstance(confdeltype, bytes):
            confdeltype = confdeltype.decode()
        assert confdeltype == 'c', (
            f'Expected ON DELETE CASCADE (confdeltype="c"), got {confdeltype!r}. '
            'Without cascade, deleting a note would leave orphan audit rows.'
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_appends_secondary_index_on_note_id_applied_at(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE indexname = 'idx_note_appends_note_id_applied_at' "
                        "AND tablename = 'note_appends'"
                    )
                )
            ).first()
        assert row is not None, (
            'Expected idx_note_appends_note_id_applied_at index for cheap per-parent audit lookups.'
        )
        defn = row[0].lower()
        assert 'note_id' in defn and 'applied_at' in defn
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_022_migration_round_trip(fresh_db_url: str) -> None:
    """upgrade head → downgrade past 022 leaves note_appends gone."""
    await _alembic_upgrade(fresh_db_url)
    await _alembic_downgrade(fresh_db_url, '021_batch_jobs_input_note_keys')

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tbl = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = 'note_appends'"
                    )
                )
            ).scalar()
            assert tbl is None, 'note_appends should be gone after downgrade.'

            idx = (
                await conn.execute(
                    text(
                        'SELECT 1 FROM pg_indexes '
                        "WHERE indexname = 'idx_note_appends_note_id_applied_at'"
                    )
                )
            ).scalar()
            assert idx is None, 'index should be gone after downgrade.'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_022_re_upgrade_after_downgrade_is_clean(fresh_db_url: str) -> None:
    """Repeating upgrade after a downgrade leaves the table back in canonical shape."""
    await _alembic_upgrade(fresh_db_url)
    await _alembic_downgrade(fresh_db_url, '021_batch_jobs_input_note_keys')
    await _alembic_upgrade(fresh_db_url)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tbl = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = 'note_appends'"
                    )
                )
            ).scalar()
            assert tbl == 1, 'note_appends should be present after re-upgrade.'
    finally:
        await engine.dispose()
