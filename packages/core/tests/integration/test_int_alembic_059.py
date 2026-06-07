"""Migration round-trip test for dropping the inbox-router schema (059).

Verifies via a real ``alembic upgrade``/``downgrade`` (not create_all) that:
- Upgrading 058 -> 059 drops the four router tables + two views.
- ``'routing'`` stays a legal ``maintenance_proposals.lint_type`` after the drop
  (the CHECK is deliberately untouched — the triage-inbox skill still emits it).
- Downgrade 059 -> 058 recreates the tables/views AND re-seeds the POC prior.
- 059 NEVER deletes ``maintenance_proposals`` rows and NEVER narrows the
  lint_type CHECK in either direction — a pre-existing routing row survives a
  full up+down round-trip, and a fresh routing row inserts cleanly afterwards.
  (This is the behaviour that differs from 055's own downgrade, which clears
  routing rows before re-narrowing the CHECK.)
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

_TARGET_BEFORE = '058_vault_summary_embedding'
_TARGET_AFTER = '059_drop_inbox_router'

_ROUTER_TABLES = (
    'inbox_router_nb_stats',
    'inbox_router_nb_class_counts',
    'inbox_router_vault_anchors',
    'inbox_router_note_cache',
)
_ROUTER_VIEWS = ('inbox_router_nb_params', 'inbox_router_nb_prior')


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig059'):
        yield url


async def _relkinds(conn, names: tuple[str, ...]) -> dict[str, str]:
    rows = (
        await conn.execute(
            text(
                'SELECT relname, relkind FROM pg_class '
                "WHERE relname = ANY(:names) AND relkind IN ('r', 'v')"
            ),
            {'names': list(names)},
        )
    ).all()

    # pg_class.relkind is Postgres's ``"char"`` type — asyncpg surfaces it as
    # ``bytes`` rather than ``str``. Normalise so plain == checks work.
    def _norm(v: object) -> str:
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

    return {r[0]: _norm(r[1]) for r in rows}


async def _make_vault(conn, name: str) -> str:
    return (
        await conn.execute(
            text('INSERT INTO vaults (id, name) VALUES (gen_random_uuid(), :n) RETURNING id'),
            {'n': name},
        )
    ).scalar()


async def _insert_routing_proposal(conn, vault_id: str) -> None:
    await conn.execute(
        text(
            'INSERT INTO maintenance_proposals '
            '(vault_id, lint_type, target_type, target_id, rule_name, '
            ' suggested_action, status, source) '
            "VALUES (:v, 'routing', 'note', gen_random_uuid()::text, "
            "'inbox_vault_route', 'route?', 'pending', 'external')"
        ),
        {'v': vault_id},
    )


@pytest.mark.asyncio
async def test_upgrade_drops_router_objects(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        # Sanity: the router objects exist at 058 (created at 055, untouched since).
        async with engine.connect() as conn:
            assert set(await _relkinds(conn, _ROUTER_TABLES)) == set(_ROUTER_TABLES)
            assert set(await _relkinds(conn, _ROUTER_VIEWS)) == set(_ROUTER_VIEWS)

        await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

        async with engine.connect() as conn:
            tables = await _relkinds(conn, _ROUTER_TABLES)
            assert tables == {}, f'router tables survived upgrade-to-059: {tables}'
            views = await _relkinds(conn, _ROUTER_VIEWS)
            assert views == {}, f'router views survived upgrade-to-059: {views}'
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_routing_lint_type_accepted_after_drop(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            vid = await _make_vault(conn, 'inbox-test-059')
            # The skill still emits routing proposals, so the CHECK must accept
            # 'routing' even though the router schema is gone.
            await _insert_routing_proposal(conn, vid)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_recreates_router_objects_and_reseeds(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = await _relkinds(conn, _ROUTER_TABLES)
            assert set(tables) == set(_ROUTER_TABLES), f'router tables not recreated: {tables}'
            assert all(k == 'r' for k in tables.values())
            views = await _relkinds(conn, _ROUTER_VIEWS)
            assert set(views) == set(_ROUTER_VIEWS), f'router views not recreated: {views}'
            assert all(k == 'v' for k in views.values())

            # POC prior re-seeded: 10 feature stat rows + 2 class rows.
            n_stats = (
                await conn.execute(text('SELECT COUNT(*) FROM inbox_router_nb_stats'))
            ).scalar()
            assert n_stats == 10
            n_class = (
                await conn.execute(text('SELECT COUNT(*) FROM inbox_router_nb_class_counts'))
            ).scalar()
            assert n_class == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_routing_rows_and_check_survive_round_trip(fresh_db_url: str) -> None:
    """059 must neither delete routing rows nor narrow the CHECK (either way)."""
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            vid = await _make_vault(conn, 'inbox-roundtrip-059')
            await _insert_routing_proposal(conn, vid)

        # Drop the router schema; the pre-existing routing row must be untouched.
        await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
        async with engine.connect() as conn:
            surviving = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM maintenance_proposals WHERE lint_type = 'routing'")
                )
            ).scalar()
            assert surviving == 1, 'upgrade-to-059 must not delete routing rows'

        # Recreate the schema; the row must STILL be there and the CHECK must
        # still accept a fresh routing insert (unlike 055's downgrade).
        await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)
        async with engine.begin() as conn:
            surviving = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM maintenance_proposals WHERE lint_type = 'routing'")
                )
            ).scalar()
            assert surviving == 1, 'downgrade-from-059 must not delete routing rows'
            vid2 = await _make_vault(conn, 'inbox-roundtrip-059-b')
            await _insert_routing_proposal(conn, vid2)  # CHECK still allows 'routing'
    finally:
        await engine.dispose()
