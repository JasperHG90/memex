"""Migration round-trip test for the entity_cooccurrences per-vault PK (052).

Verifies that:
- Upgrading to ``052_entity_cooccurrence_vault_pk`` changes the primary key from
  ``(entity_id_1, entity_id_2)`` to ``(entity_id_1, entity_id_2, vault_id)``.
- Downgrading restores the two-column PK (rollback parity).
- The upgrade rebuilds the table from ground truth so the SAME entity pair
  co-mentioned in two different vaults yields TWO independent rows with the
  correct per-vault counts — the core fix for the cross-vault corruption.
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

_TARGET_BEFORE = '051_fix_telemetry_pk'
_TARGET_AFTER = '052_entity_cooccurrence_vault_pk'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig052'):
        yield url


async def _pk_columns(conn) -> list[str]:
    """Return the entity_cooccurrences primary-key column names, in key order."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                WHERE c.conrelid = 'entity_cooccurrences'::regclass
                  AND c.contype = 'p'
                ORDER BY array_position(c.conkey, a.attnum)
                """
            )
        )
    ).all()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_upgrade_makes_pk_per_vault(fresh_db_url: str) -> None:
    """Upgrade through 052 → PK becomes (entity_id_1, entity_id_2, vault_id)."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _pk_columns(conn) == ['entity_id_1', 'entity_id_2'], (
                'precondition: PK should be two-column before 052'
            )
    finally:
        await engine.dispose()

    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _pk_columns(conn) == ['entity_id_1', 'entity_id_2', 'vault_id']
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_restores_two_column_pk(fresh_db_url: str) -> None:
    """Downgrade past 052 restores the two-column PK for rollback parity."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await _pk_columns(conn) == ['entity_id_1', 'entity_id_2']
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_rebuilds_per_vault_counts(fresh_db_url: str) -> None:
    """The rebuild yields one row per (pair, vault) from ground truth.

    Seed the same entity pair co-mentioned in two vaults, plus a single stale
    pre-052 cooccurrence row (all that the old 2-column PK could hold). After
    upgrade, expect two rows — one per vault — each counting that vault's units.
    """
    # Pre-052 schema (two-column PK).
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)

    vault_a, vault_b = uuid4(), uuid4()
    e1, e2 = sorted([uuid4(), uuid4()])  # canonical order (entity_id_1 < entity_id_2)
    note_a, note_b = uuid4(), uuid4()
    unit_a, unit_b = uuid4(), uuid4()

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for vid, name in ((vault_a, 'vault-a'), (vault_b, 'vault-b')):
                await conn.execute(
                    text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
                    {'id': str(vid), 'name': f'{name}-{uuid4().hex[:6]}'},
                )
            for eid in (e1, e2):
                await conn.execute(
                    text(
                        'INSERT INTO entities (id, canonical_name, mention_count) '
                        'VALUES (:id, :n, 2)'
                    ),
                    {'id': str(eid), 'n': f'Ent-{uuid4().hex[:8]}'},
                )
            for nid, vid in ((note_a, vault_a), (note_b, vault_b)):
                await conn.execute(
                    text(
                        'INSERT INTO notes (id, vault_id, original_text, content_hash) '
                        'VALUES (:id, :v, :t, :h)'
                    ),
                    {'id': str(nid), 'v': str(vid), 't': 'text', 'h': uuid4().hex},
                )
            for uid, nid, vid in ((unit_a, note_a, vault_a), (unit_b, note_b, vault_b)):
                await conn.execute(
                    text(
                        'INSERT INTO memory_units '
                        '(id, vault_id, note_id, text, fact_type, event_date, status) '
                        "VALUES (:id, :v, :n, :txt, 'world', now(), 'active')"
                    ),
                    {'id': str(uid), 'v': str(vid), 'n': str(nid), 'txt': f'f {uuid4().hex}'},
                )
                # Each unit co-mentions BOTH entities.
                for eid in (e1, e2):
                    await conn.execute(
                        text(
                            'INSERT INTO unit_entities (unit_id, entity_id, vault_id) '
                            'VALUES (:u, :e, :v)'
                        ),
                        {'u': str(uid), 'e': str(eid), 'v': str(vid)},
                    )
            # Stale pre-052 row: the 2-column PK can hold only ONE row for the pair.
            await conn.execute(
                text(
                    'INSERT INTO entity_cooccurrences '
                    '(entity_id_1, entity_id_2, vault_id, cooccurrence_count, last_cooccurred) '
                    'VALUES (:e1, :e2, :v, 99, now())'
                ),
                {'e1': str(e1), 'e2': str(e2), 'v': str(vault_a)},
            )
    finally:
        await engine.dispose()

    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        'SELECT vault_id, cooccurrence_count FROM entity_cooccurrences '
                        'WHERE entity_id_1 = :e1 AND entity_id_2 = :e2'
                    ),
                    {'e1': str(e1), 'e2': str(e2)},
                )
            ).all()
            by_vault = {str(r[0]): r[1] for r in rows}
            # Two independent per-vault rows, each counting that vault's single
            # co-mentioning unit — the stale count=99 is discarded by the rebuild.
            assert by_vault == {str(vault_a): 1, str(vault_b): 1}
    finally:
        await engine.dispose()
