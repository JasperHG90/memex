"""Migration round-trip test for the inbox-router schema (055).

Verifies via a real ``alembic upgrade`` (not create_all) that:
- The four router tables + two views are created.
- The sufficient-stats prior is seeded (10 feature rows + 2 class rows) and the
  derived params view yields sane (μ, σ²).
- The ``maintenance_proposals`` lint_type CHECK accepts ``'routing'``.
- Downgrade drops the router objects and reverts the CHECK.
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

_TARGET_BEFORE = '054_nodes_vault_active'
_TARGET_AFTER = '055_inbox_router'

_ROUTER_TABLES = (
    'inbox_router_nb_stats',
    'inbox_router_nb_class_counts',
    'inbox_router_vault_anchors',
    'inbox_router_note_cache',
)
_ROUTER_VIEWS = ('inbox_router_nb_params', 'inbox_router_nb_prior')


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig054'):
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
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_upgrade_creates_router_objects_and_seeds_prior(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_BEFORE)
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = await _relkinds(conn, _ROUTER_TABLES)
            assert set(tables) == set(_ROUTER_TABLES), f'missing router tables: {tables}'
            assert all(k == 'r' for k in tables.values())

            views = await _relkinds(conn, _ROUTER_VIEWS)
            assert set(views) == set(_ROUTER_VIEWS), f'missing router views: {views}'
            assert all(k == 'v' for k in views.values())

            # Seeded prior: 10 feature stat rows + 2 class-count rows.
            n_stats = (
                await conn.execute(text('SELECT COUNT(*) FROM inbox_router_nb_stats'))
            ).scalar()
            assert n_stats == 10
            n_class = (
                await conn.execute(text('SELECT COUNT(*) FROM inbox_router_nb_class_counts'))
            ).scalar()
            assert n_class == 2

            # The params view yields positive, finite variances and sane means.
            rows = (
                await conn.execute(
                    text(
                        'SELECT feature_name, label, mu, sigma_sq '
                        'FROM inbox_router_nb_params ORDER BY feature_name, label'
                    )
                )
            ).all()
            assert len(rows) == 10
            for _f, _label, mu, sigma_sq in rows:
                assert sigma_sq > 0.0
                assert 0.0 <= mu <= 1.0

            # match-class prior < no-match (1:6 base rate), so log_prior(1) < log_prior(0).
            lp = dict(
                (r[0], r[1])
                for r in (
                    await conn.execute(text('SELECT label, log_prior FROM inbox_router_nb_prior'))
                ).all()
            )
            assert lp[1] < lp[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_routing_lint_type_accepted_after_upgrade(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            vid = (
                await conn.execute(
                    text(
                        'INSERT INTO vaults (id, name) '
                        "VALUES (gen_random_uuid(), 'inbox-test') RETURNING id"
                    )
                )
            ).scalar()
            # 'routing' lint_type must satisfy the CHECK.
            await conn.execute(
                text(
                    'INSERT INTO maintenance_proposals '
                    '(vault_id, lint_type, target_type, target_id, rule_name, '
                    ' suggested_action, status, source) '
                    "VALUES (:v, 'routing', 'note', gen_random_uuid()::text, "
                    "'inbox_vault_route', 'route?', 'pending', 'rule')"
                ),
                {'v': vid},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_router_objects(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, target=_TARGET_AFTER)
    await _alembic_downgrade(fresh_db_url, _TARGET_BEFORE)
    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = await _relkinds(conn, _ROUTER_TABLES)
            assert tables == {}, f'router tables survived downgrade: {tables}'
            views = await _relkinds(conn, _ROUTER_VIEWS)
            assert views == {}, f'router views survived downgrade: {views}'
    finally:
        await engine.dispose()
