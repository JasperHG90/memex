"""Migration round-trip test for vault kind + policy (060).

Verifies via a real ``alembic upgrade`` / ``downgrade`` against a populated DB:
- 060 adds ``kind`` (default 'content') + ``policy`` (default '{}') to ``vaults``.
- A pre-existing vault backfills to ``content`` with empty policy.
- A pre-existing ``inbox`` vault flips to ``system`` AND its prior mental models
  are archived + its vault summary deleted (now-stale synthesis).
- The CHECK constraint rejects an invalid kind.
- Downgrade drops both columns.
"""

from __future__ import annotations

from typing import AsyncGenerator

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

_BEFORE = '059_drop_inbox_router'
_AFTER = '060_vault_kind_policy'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig060'):
        yield url


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


async def _seed_at_058(db_url: str) -> dict[str, str]:
    """Upgrade to 058; seed a content vault and an inbox vault with synthesis."""
    await _alembic_upgrade(db_url, target=_BEFORE)
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            content_id = (
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name) '
                        "VALUES (gen_random_uuid(), 'corpus-059') RETURNING id"
                    )
                )
            ).scalar()
            inbox_id = (
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name) '
                        "VALUES (gen_random_uuid(), 'inbox') RETURNING id"
                    )
                )
            ).scalar()
            # An entity + a mental model + a vault summary in the inbox vault.
            entity_id = (
                await conn.execute(
                    text(
                        'INSERT INTO entities (id, canonical_name, entity_type) '
                        "VALUES (gen_random_uuid(), 'Acme', 'ORG') RETURNING id"
                    )
                )
            ).scalar()
            await conn.execute(
                text(
                    'INSERT INTO mental_models '
                    '(id, entity_id, vault_id, name, observations, entity_metadata, version) '
                    "VALUES (gen_random_uuid(), :e, :v, 'Acme', '[]'::jsonb, '{}'::jsonb, 1)"
                ),
                {'e': entity_id, 'v': inbox_id},
            )
            await conn.execute(
                text(
                    'INSERT INTO vault_summaries '
                    '(id, vault_id, narrative, themes, inventory, key_entities, '
                    ' version, notes_incorporated, patch_log) '
                    "VALUES (gen_random_uuid(), :v, 'inbox narrative', "
                    "'[]'::jsonb, '{}'::jsonb, '[]'::jsonb, 1, 1, '[]'::jsonb)"
                ),
                {'v': inbox_id},
            )
        return {'content': str(content_id), 'inbox': str(inbox_id)}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_adds_columns_and_backfills_content(fresh_db_url: str) -> None:
    ids = await _seed_at_058(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _column_exists(conn, 'vaults', 'kind')
            assert await _column_exists(conn, 'vaults', 'policy')
            kind, policy = (
                await conn.execute(
                    text('SELECT kind, policy FROM vaults WHERE id = :v'),
                    {'v': ids['content']},
                )
            ).one()
            assert kind == 'content'
            assert policy == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_flips_inbox_and_archives_synthesis(fresh_db_url: str) -> None:
    ids = await _seed_at_058(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            kind = (
                await conn.execute(
                    text('SELECT kind FROM vaults WHERE id = :v'), {'v': ids['inbox']}
                )
            ).scalar()
            assert kind == 'system'

            archived = (
                await conn.execute(
                    text('SELECT archived_at FROM mental_models WHERE vault_id = :v'),
                    {'v': ids['inbox']},
                )
            ).scalar()
            assert archived is not None  # prior synthesis archived

            summary_count = (
                await conn.execute(
                    text('SELECT count(*) FROM vault_summaries WHERE vault_id = :v'),
                    {'v': ids['inbox']},
                )
            ).scalar()
            assert summary_count == 0  # stale summary deleted
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_rejects_bad_kind(fresh_db_url: str) -> None:
    await _seed_at_058(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name, kind) '
                        "VALUES (gen_random_uuid(), 'bad-kind-059', 'archive')"
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_columns(fresh_db_url: str) -> None:
    await _seed_at_058(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_AFTER)
    await _alembic_downgrade(fresh_db_url, _BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert not await _column_exists(conn, 'vaults', 'kind')
            assert not await _column_exists(conn, 'vaults', 'policy')
    finally:
        await engine.dispose()
