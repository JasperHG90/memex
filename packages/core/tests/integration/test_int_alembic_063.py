"""Integration tests for migration 063_experiential_seed.

Verifies via real ``alembic upgrade`` / ``downgrade`` against a Postgres
testcontainer:

- 063 seeds a hidden ``experiential`` system vault with the right
  ``kind='system'`` and ``policy={'hidden': true}``;
- a pre-existing ``<scope>:procedure:<verb>:<context>`` KV row is
  backfilled into ``experiential_entries`` as a draft procedure, with
  ``origin='kv_backfill'`` and a sidecar row in
  ``_migrated_kv_procedures_063`` recording the link;
- the backfill is idempotent (re-running upgrade does not duplicate
  rows);
- downgrade removes the seeded vault and the backfilled entries, but
  leaves the original KV row untouched.
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

_TARGET = '063_experiential_seed'
_DOWN = '062_notes_role'
_VAULT_NAME = 'experiential'
_SIDECAR = '_migrated_kv_procedures_063'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig063'):
        yield url


async def _seed_kv_at_062(db_url: str) -> dict[str, str]:
    """Upgrade to 062, then insert a legacy procedure KV row."""
    await _alembic_upgrade(db_url, target=_DOWN)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # Two legacy procedure rows: one with context, one without.
            await conn.execute(
                text(
                    'INSERT INTO kv_entries (key, value) '
                    "VALUES ('global:procedure:create_alembic:postgres', "
                    "        'run alembic revision -m msg')"
                )
            )
            await conn.execute(
                text(
                    'INSERT INTO kv_entries (key, value) '
                    "VALUES ('project:demo:procedure:lint', 'run ruff check')"
                )
            )
        return {
            'with_ctx': 'global:procedure:create_alembic:postgres',
            'no_ctx': 'project:demo:procedure:lint',
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_seeds_experiential_system_vault(fresh_db_url: str) -> None:
    """After upgrade, the ``experiential`` system vault exists with
    kind='system' and policy={'hidden': true}."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT kind, policy FROM vaults WHERE name = :n'),
                    {'n': _VAULT_NAME},
                )
            ).first()
            assert row is not None, f'seeded vault {_VAULT_NAME!r} missing after 063 upgrade'
            kind, policy = row
            assert kind == 'system', f'expected kind=system, got {kind!r}'
            assert policy == {'hidden': True}, f'expected policy={{"hidden": true}}, got {policy!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_backfills_legacy_kv_procedures(fresh_db_url: str) -> None:
    """Legacy ``<scope>:procedure:*`` KV rows are duplicated as draft
    ``experiential_entries`` rows with ``origin='kv_backfill'`` and a
    sidecar row in ``_migrated_kv_procedures_063``."""
    keys = await _seed_kv_at_062(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            vault_id = (
                await conn.execute(
                    text('SELECT id FROM vaults WHERE name = :n'), {'n': _VAULT_NAME}
                )
            ).scalar()
            assert vault_id is not None

            entries = (
                await conn.execute(
                    text(
                        'SELECT id, kind, scope, verb, context, status, origin '
                        'FROM experiential_entries '
                        "WHERE origin = 'kv_backfill' "
                        'ORDER BY scope, verb'
                    )
                )
            ).fetchall()

            assert len(entries) == 2, (
                f'expected 2 backfilled experiential_entries rows, got {len(entries)}: {entries!r}'
            )

            by_key = {tuple(row[2:6]): row for row in entries}
            # global:procedure:create_alembic:postgres → procedure,
            # scope='global', verb='create_alembic', context='postgres'.
            global_entry = by_key.get(('global', 'create_alembic', 'postgres'))
            assert global_entry is not None, (
                f'expected backfill for {keys["with_ctx"]!r}; got {entries!r}'
            )
            assert global_entry[1] == 'procedure'  # kind
            assert global_entry[5] == 'draft'  # status
            assert global_entry[6] == 'kv_backfill'  # origin

            # project:demo:procedure:lint → procedure, scope='project:demo',
            # verb='lint', context=None.
            project_entry = by_key.get(('project:demo', 'lint', None))
            assert project_entry is not None, (
                f'expected backfill for {keys["no_ctx"]!r} with NULL context; got {entries!r}'
            )
            assert project_entry[1] == 'procedure'
            assert project_entry[5] == 'draft'
            assert project_entry[6] == 'kv_backfill'

            # Sidecar rows link each KV key to its derived entry.
            sidecar = (
                await conn.execute(text(f'SELECT kv_key, entry_id FROM {_SIDECAR} ORDER BY kv_key'))
            ).fetchall()
            assert len(sidecar) == 2, f'expected 2 sidecar rows, got {len(sidecar)!r}'
            sidecar_keys = {row[0] for row in sidecar}
            assert sidecar_keys == {keys['with_ctx'], keys['no_ctx']}, (
                f'sidecar kv_keys mismatch: expected '
                f'{{{keys["with_ctx"]!r}, {keys["no_ctx"]!r}}}, got {sidecar_keys!r}'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(fresh_db_url: str) -> None:
    """The KV backfill is round-trip-safe: downgrade cleans the derived
    rows and the sidecar, and a re-upgrade produces exactly the same
    set (one draft per legacy KV procedure row, one sidecar row each).
    The sidecar's ``UNIQUE(kv_key)`` constraint guarantees idempotency
    on the *sidecar* path; for the entries table, the deterministic
    ``(kind, scope, verb, context)`` UNIQUE NULLS NOT DISTINCT on
    ``experiential_entries`` is the upsert key if a re-run re-derives
    without going through the sidecar (defence-in-depth)."""
    await _seed_kv_at_062(fresh_db_url)

    # First upgrade seeds the backfill.
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    # Downgrade removes backfilled rows + sidecar + vault; legacy KV rows remain.
    await _alembic_downgrade(fresh_db_url, _DOWN)
    # A second upgrade must reproduce the same 2 backfilled entries.
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM experiential_entries WHERE origin = 'kv_backfill'")
                )
            ).scalar()
            assert count == 2, (
                f'expected 2 backfilled entries after downgrade+upgrade round, got {count!r}'
            )
            sidecar_count = (await conn.execute(text(f'SELECT count(*) FROM {_SIDECAR}'))).scalar()
            assert sidecar_count == 2, f'expected 2 sidecar rows after round, got {sidecar_count!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_removes_seeded_vault_and_backfill_keeps_kv(
    fresh_db_url: str,
) -> None:
    """Downgrade past 063 drops the seeded vault and the backfilled
    experiential_entries rows, but the original KV row stays."""
    keys = await _seed_kv_at_062(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            vault_count = (
                await conn.execute(
                    text('SELECT count(*) FROM vaults WHERE name = :n'),
                    {'n': _VAULT_NAME},
                )
            ).scalar()
            assert vault_count == 0, (
                f'expected seeded vault {_VAULT_NAME!r} gone after 063 downgrade, '
                f'found {vault_count!r}'
            )
            backfill_count = (
                await conn.execute(
                    text("SELECT count(*) FROM experiential_entries WHERE origin = 'kv_backfill'")
                )
            ).scalar()
            assert backfill_count == 0, (
                f'expected backfilled rows gone after 063 downgrade, found {backfill_count!r}'
            )

            # Original KV row must still exist — downgrade does not delete legacy data.
            kv_count = (
                await conn.execute(
                    text('SELECT count(*) FROM kv_entries WHERE key = :k'),
                    {'k': keys['with_ctx']},
                )
            ).scalar()
            assert kv_count == 1, (
                f'legacy KV row {keys["with_ctx"]!r} should survive 063 downgrade, '
                f'found {kv_count!r}'
            )
    finally:
        await engine.dispose()
