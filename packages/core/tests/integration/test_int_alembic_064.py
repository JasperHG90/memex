"""Integration test for migration 064_two_kind_plane's data conformance.

Pins the abort-hazard fix: 064 promotes user-scoped entries to ``global``
and NULLs strategy contexts. The original UPDATEs evaluated their
``NOT EXISTS`` guard against the pre-update snapshot, so two rows that would
land on the same anchor both flipped and collided on
``uq_procedural_identity`` mid-statement — aborting the whole migration.
The window-dedup fix promotes only ONE representative per anchor; the rest
are deleted. This test seeds exactly those colliding shapes at rev 063 and
asserts 064 applies cleanly, leaving a single unique row per anchor.
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import (  # noqa: F401
    alembic_upgrade as _alembic_upgrade,
    make_fresh_db,
)

pytestmark = [pytest.mark.integration]

_BEFORE = '063_experiential_seed'
_TARGET = '064_two_kind_plane'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig064'):
        yield url


async def _seed_collisions(url: str) -> None:
    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.begin() as conn:
            vid = uuid4()
            await conn.execute(
                text('INSERT INTO vaults (id, name) VALUES (:id, :n)'),
                {'id': str(vid), 'n': f'mig064_{vid.hex[:8]}'},
            )
            # Two user-scoped procedures that collide on (procedure, global,
            # deploy, nomad) the instant 064 flips both scopes to 'global'.
            for scope in ('user:alice', 'user:bob'):
                await conn.execute(
                    text(
                        'INSERT INTO experiential_entries '
                        '(vault_id, kind, scope, verb, context, title, summary, trigger) '
                        "VALUES (:v, 'procedure', :s, 'deploy', 'nomad', 't', 's', 'when deploy')"
                    ),
                    {'v': str(vid), 's': scope},
                )
            # Two strategies sharing (global, release) with distinct contexts —
            # at rev 063 ck_strategy_context REQUIRES a non-NULL context, so both
            # are legal here; 064 collapses both to context=NULL and they would
            # collide on (strategy, global, release, NULL). The window-dedup keeps
            # one.
            for ctx in ('blue', 'green'):
                await conn.execute(
                    text(
                        'INSERT INTO experiential_entries '
                        '(vault_id, kind, scope, verb, context, title, summary, trigger) '
                        "VALUES (:v, 'strategy', 'global', 'release', :c, 't', 's', 'when release')"
                    ),
                    {'v': str(vid), 'c': ctx},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_064_survives_colliding_anchors(fresh_db_url: str) -> None:
    await _alembic_upgrade(fresh_db_url, _BEFORE)
    await _seed_collisions(fresh_db_url)

    # The load-bearing assertion: this no longer raises a unique-violation
    # abort. (Pre-fix, the snapshot-evaluated UPDATE collided here.)
    await _alembic_upgrade(fresh_db_url, _TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            procs = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM procedural_entries WHERE kind='procedure' "
                        "AND scope='global' AND verb='deploy' AND context='nomad'"
                    )
                )
            ).scalar()
            strats = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM procedural_entries WHERE kind='strategy' "
                        "AND scope='global' AND verb='release' AND context IS NULL"
                    )
                )
            ).scalar()
    finally:
        await engine.dispose()

    # Exactly one representative survived each anchor; the dup was deleted.
    assert procs == 1
    assert strats == 1
