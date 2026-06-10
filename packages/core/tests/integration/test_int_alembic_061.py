"""Integration tests for migration 061_experiential_entries.

Runs ``alembic upgrade`` against a real Postgres testcontainer and asserts:

- the 5 experiential tables land with the right column shape, CHECK
  constraints, and partial unique indexes;
- the HNSW indexes exist on the vector columns with ``vector_cosine_ops``;
- the generated ``search_tsvector`` column is populated and reachable via
  GIN;
- the round-trip (``upgrade`` then ``downgrade``) cleans up every
  table, index, and constraint.
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

_TARGET = '061_experiential_entries'
_DOWN = '060_vault_kind_policy'

_EXPECTED_TABLES = (
    'experiential_entries',
    'experiential_entry_versions',
    'experiential_sources',
    'experiential_pins',
    'experiential_derivation_queue',
)

_EXPECTED_CHECKS: dict[str, tuple[str, ...]] = {
    'experiential_entries': (
        'ck_experiential_kind',
        'ck_experiential_status',
        'ck_experiential_origin',
        'ck_strategy_context',
        'ck_experiential_body_embedding_scope',
    ),
    'experiential_sources': (
        'ck_experiential_sources_role',
        'ck_experiential_sources_weight',
        'ck_experiential_sources_pointer_set',
    ),
    'experiential_pins': ('ck_experiential_pins_position_nonneg',),
    'experiential_derivation_queue': (
        'ck_derivation_queue_target_kind',
        'ck_derivation_queue_status',
        'ck_derivation_queue_attempt_nonneg',
        'ck_derivation_queue_strategy_context',
    ),
}


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig061'):
        yield url


async def _check_exists(conn, name: str) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    'SELECT 1 FROM information_schema.table_constraints '
                    "WHERE constraint_name = :n AND constraint_type = 'CHECK'"
                ),
                {'n': name},
            )
        ).scalar()
    )


async def _index_exists(conn, name: str) -> bool:
    return bool(
        (
            await conn.execute(
                text('SELECT 1 FROM pg_indexes WHERE indexname = :n'),
                {'n': name},
            )
        ).scalar()
    )


async def _table_exists(conn, name: str) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    'SELECT 1 FROM information_schema.tables '
                    'WHERE table_schema = current_schema() AND table_name = :n'
                ),
                {'n': name},
            )
        ).scalar()
    )


async def _hnsw_opclass(conn, index_name: str) -> str | None:
    row = (
        await conn.execute(
            text(
                """
                SELECT op.opcname
                FROM pg_index ix
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_opclass op ON op.oid = ix.indclass[0]
                WHERE i.relname = :name
                """
            ),
            {'name': index_name},
        )
    ).first()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_upgrade_creates_all_five_tables_with_checks(fresh_db_url: str) -> None:
    """All 5 experiential tables land, each with its expected CHECKs."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            for table in _EXPECTED_TABLES:
                assert await _table_exists(conn, table), (
                    f'expected table {table!r} to exist after 061 upgrade'
                )
            for table, checks in _EXPECTED_CHECKS.items():
                for check in checks:
                    assert await _check_exists(conn, check), (
                        f'expected CHECK constraint {check!r} on {table!r} after 061 upgrade'
                    )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_creates_hnsw_and_gin_indexes(fresh_db_url: str) -> None:
    """HNSW on both embedding columns (with vector_cosine_ops) and GIN on
    the generated tsvector — plus the identity-anchor unique index."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            # Partial unique identity anchor.
            assert await _index_exists(conn, 'uq_experiential_identity'), (
                'uq_experiential_identity UNIQUE index missing on experiential_entries'
            )
            # GIN on generated tsvector.
            assert await _index_exists(conn, 'idx_experiential_entries_search_tsvector'), (
                'GIN on search_tsvector missing'
            )
            gin_def = (
                await conn.execute(
                    text(
                        'SELECT indexdef FROM pg_indexes '
                        "WHERE indexname = 'idx_experiential_entries_search_tsvector'"
                    )
                )
            ).scalar()
            assert gin_def is not None and 'using gin' in gin_def.lower(), (
                f'idx_experiential_entries_search_tsvector should be GIN, got {gin_def!r}'
            )

            # HNSW on body_embedding (procedures/strategies) — partial WHERE
            # status='published' AND kind IN ('procedure','strategy').
            assert await _index_exists(conn, 'idx_experiential_entries_body_embedding'), (
                'HNSW index on body_embedding missing'
            )
            body_op = await _hnsw_opclass(conn, 'idx_experiential_entries_body_embedding')
            assert body_op == 'vector_cosine_ops', (
                f'expected vector_cosine_ops on body_embedding HNSW, got {body_op!r}'
            )

            # HNSW on trigger_embedding (cases) — partial WHERE kind='case'.
            assert await _index_exists(conn, 'idx_experiential_entries_trigger_embedding'), (
                'HNSW index on trigger_embedding missing'
            )
            trig_op = await _hnsw_opclass(conn, 'idx_experiential_entries_trigger_embedding')
            assert trig_op == 'vector_cosine_ops', (
                f'expected vector_cosine_ops on trigger_embedding HNSW, got {trig_op!r}'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_persists_generated_tsvector_and_index_round_trip(
    fresh_db_url: str,
) -> None:
    """After upgrade, inserting a row populates ``search_tsvector`` and
    the GIN index makes it reachable via ``@@``."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # Seed a vault row so the FK on experiential_entries.vault_id has a target.
            vault_id = (
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name, kind) '
                        "VALUES (gen_random_uuid(), 'mig061-vault', 'content') RETURNING id"
                    )
                )
            ).scalar()
            entry_id = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO experiential_entries
                            (vault_id, kind, scope, title, summary, body, tags)
                        VALUES
                            (:v, 'procedure', 'global', 'create alembic migration',
                             'how to add a new alembic migration to the project',
                             'run `alembic revision -m "..."` then write upgrade()',
                             ARRAY['alembic','postgres'])
                        RETURNING id
                        """
                    ),
                    {'v': vault_id},
                )
            ).scalar()

            # The generated tsvector should now contain 'alembic' (or its
            # Porter-stemmed form 'alemb' under the english config).
            tsvector_value = (
                await conn.execute(
                    text('SELECT search_tsvector FROM experiential_entries WHERE id = :id'),
                    {'id': entry_id},
                )
            ).scalar()
            assert tsvector_value is not None, 'search_tsvector should be auto-populated on insert'
            tsvector_str = str(tsvector_value)
            assert 'alemb' in tsvector_str, (
                f"expected 'alemb' (stemmed form of 'alembic') in generated "
                f'tsvector, got {tsvector_str!r}'
            )

            # GIN reachable: a tsquery on the stem matches.
            matches = (
                await conn.execute(
                    text(
                        'SELECT count(*) FROM experiential_entries '
                        "WHERE search_tsvector @@ to_tsquery('english', 'alemb')"
                    )
                )
            ).scalar()
            assert matches == 1, f'GIN-backed tsvector match should return 1, got {matches!r}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_all_tables_and_indexes(fresh_db_url: str) -> None:
    """Downgrade past 061 removes every table, CHECK, and index it added."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET)
    await _alembic_downgrade(fresh_db_url, _DOWN)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            for table in _EXPECTED_TABLES:
                assert not await _table_exists(conn, table), (
                    f'expected table {table!r} to be gone after 061 downgrade'
                )
            for checks in _EXPECTED_CHECKS.values():
                for check in checks:
                    assert not await _check_exists(conn, check), (
                        f'expected CHECK {check!r} to be gone after 061 downgrade'
                    )
            for index in (
                'uq_experiential_identity',
                'idx_experiential_entries_search_tsvector',
                'idx_experiential_entries_body_embedding',
                'idx_experiential_entries_trigger_embedding',
            ):
                assert not await _index_exists(conn, index), (
                    f'expected index {index!r} to be gone after 061 downgrade'
                )
    finally:
        await engine.dispose()
