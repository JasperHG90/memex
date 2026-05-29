"""Integration tests for InboxRouterService against real Postgres.

The shared test harness builds schema via ``SQLModel.metadata.create_all``,
which does not create the router's migration-only tables/views. The
``router_schema`` fixture below runs the migration's DDL + seed so the service
has its tables. Distinct per-vault chunk embeddings give a real ranking signal
(the mock embedding model returns a constant vector for narratives).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from memex_core.memory.sql_models import Chunk, ContentStatus, Note, Vault
from memex_core.services.inbox_router.service import ROUTE_RULE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Two orthogonal embedding directions so cosine cleanly separates the vaults.
_VEC_A = [1.0] * 192 + [0.0] * 192
_VEC_B = [0.0] * 192 + [1.0] * 192


def _import_migration_ddl() -> tuple[str, str, str]:
    import importlib.util
    import pathlib as plb

    path = (
        plb.Path(__file__).resolve().parents[2]
        / 'src/memex_core/alembic/versions/055_inbox_router.py'
    )
    spec = importlib.util.spec_from_file_location('mig054_ddl', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._CREATE, mod._SEED_STATS, mod._SEED_CLASS


@pytest_asyncio.fixture
async def router_schema(api):
    """Create + seed the router tables/views (idempotent per test)."""
    create_sql, seed_stats, seed_class = _import_migration_ddl()
    drop_sql = (
        'DROP TABLE IF EXISTS inbox_router_note_cache CASCADE;'
        'DROP TABLE IF EXISTS inbox_router_vault_anchors CASCADE;'
        'DROP VIEW IF EXISTS inbox_router_nb_prior CASCADE;'
        'DROP VIEW IF EXISTS inbox_router_nb_params CASCADE;'
        'DROP TABLE IF EXISTS inbox_router_nb_class_counts CASCADE;'
        'DROP TABLE IF EXISTS inbox_router_nb_stats CASCADE;'
    )

    async def _run_script(session, script: str) -> None:
        for stmt in (s.strip() for s in script.split(';')):
            if stmt:
                await session.execute(text(stmt))

    async with api.metastore.session() as session:
        await _run_script(session, drop_sql)
        await _run_script(session, create_sql)
        await _run_script(session, seed_stats)
        await _run_script(session, seed_class)
        await session.commit()
    yield
    async with api.metastore.session() as session:
        await _run_script(session, drop_sql)
        await session.commit()


async def _seed_vault_with_note(session, name: str, embedding: list[float]) -> Vault:
    vault = Vault(id=uuid4(), name=name, description=f'{name} vault')
    session.add(vault)
    note_id = uuid4()
    session.add(
        Note(
            id=note_id,
            vault_id=vault.id,
            original_text=f'note {uuid4().hex}',
            content_hash=uuid4().hex,
            title=f'{name} note',
        )
    )
    session.add(
        Chunk(
            id=uuid4(),
            vault_id=vault.id,
            note_id=note_id,
            text=f'{name} body content alpha beta',
            content_hash=uuid4().hex,
            embedding=embedding,
            chunk_index=0,
            status=ContentStatus.ACTIVE,
        )
    )
    await session.commit()
    return vault


async def _seed_inbox_note(session, inbox: Vault, embedding: list[float]) -> str:
    note_id = uuid4()
    session.add(
        Note(
            id=note_id,
            vault_id=inbox.id,
            original_text=f'inbox note {uuid4().hex}',
            content_hash=uuid4().hex,
            title='inbox note',
        )
    )
    session.add(
        Chunk(
            id=uuid4(),
            vault_id=inbox.id,
            note_id=note_id,
            text='inbox body content alpha beta',
            content_hash=uuid4().hex,
            embedding=embedding,
            chunk_index=0,
            status=ContentStatus.ACTIVE,
        )
    )
    await session.commit()
    return str(note_id)


async def test_score_ranks_topically_closest_vault_first(api, session, router_schema):
    """An inbox note embedded like vault-a should rank vault-a above vault-b."""
    inbox = await _seed_vault_with_note(session, 'inbox', _VEC_A)
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)

    await api.inbox_router.refresh_anchors()
    await api.inbox_router.populate_note_cache(note_id)
    scored = await api.inbox_router.score_notes([note_id])

    cands = scored[UUID(note_id)]
    names = [c.vault_name for c in sorted(cands, key=lambda c: -c.p_match)]
    assert 'vault-a' in names and 'vault-b' in names
    # vault-a shares the note's embedding direction; it must outrank vault-b.
    assert names.index('vault-a') < names.index('vault-b')


async def test_triage_tick_emits_routing_proposal(api, session, router_schema):
    """A full tick scores the inbox note and records a routing proposal."""
    inbox = await _seed_vault_with_note(session, 'inbox', _VEC_A)
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)

    result = await api.inbox_router.triage_tick()
    assert result.scored == 1
    assert result.errors == 0

    async with api.metastore.session() as s:
        n = (
            await s.execute(
                text(
                    'SELECT COUNT(*) FROM maintenance_proposals '
                    "WHERE lint_type = 'routing' AND target_id = :tid"
                ),
                {'tid': note_id},
            )
        ).scalar()
    # Cold start (seed match-count=1 < 50) → a pending route proposal, not auto-route.
    assert n >= 1


async def test_record_feedback_updates_sufficient_stats(api, session, router_schema):
    """An online update increments the match class count and a feature's n."""
    inbox = await _seed_vault_with_note(session, 'inbox', _VEC_A)
    vault_a = await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)
    await api.inbox_router.refresh_anchors()
    await api.inbox_router.populate_note_cache(note_id)

    async with api.metastore.session() as s:
        before = (
            await s.execute(text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1'))
        ).scalar()

    await api.inbox_router.record_feedback(UUID(note_id), vault_a.id, 1)

    async with api.metastore.session() as s:
        after = (
            await s.execute(text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1'))
        ).scalar()
    # With EWMA gamma<1: n_after = gamma*n_before + 1.
    gamma = api.config.server.memory.inbox_router.ewma_gamma
    assert after == pytest.approx(gamma * float(before) + 1.0, rel=1e-6)


async def test_route_rule_name_constant():
    assert ROUTE_RULE == 'inbox_vault_route'
