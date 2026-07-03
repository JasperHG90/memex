"""Runtime validation that the perf-fixed note-graph SQL executes on real Postgres.

Compile-level tests pin the SHAPE (pg_trgm ``%`` operator, neighbour LIMIT); this
pins that the generated SQL actually RUNS against Postgres — the ``%`` operator
requires the ``pg_trgm`` extension, and the capped ``UNION ALL ... ORDER BY ...
LIMIT`` neighbour CTE must be valid SQL. Executing without error is the gate.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig
from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceNoteGraphStrategy,
    build_seed_entity_cte,
)
from memex_core.memory.sql_models import (
    EMBEDDING_DIMENSION,
    Chunk,
    ContentStatus,
    Entity,
    EntityCooccurrence,
    MemoryUnit,
    Note,
    UnitEntity,
    Vault,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_note_graph_capped_statement_executes(
    session: AsyncSession, memex_config: MemexConfig
) -> None:
    vault = Vault(id=uuid4(), name=f'note-graph-perf-{uuid4().hex[:8]}')
    entity = Entity(id=uuid4(), canonical_name='mcp servers')
    session.add_all([vault, entity])
    await session.commit()

    strat = EntityCooccurrenceNoteGraphStrategy(
        max_neighbors=memex_config.server.memory.retrieval.graph_max_neighbors,
        enable_semantic_seeding=False,
    )
    stmt = strat.get_statement('mcp servers', None, limit=60, vault_ids=[vault.id])

    # Must execute without error: exercises the pg_trgm `%` seed predicate AND the
    # capped neighbour CTE against real Postgres. No chunks seeded → 0 rows is fine.
    result = await session.execute(stmt)
    rows = result.all()
    assert isinstance(rows, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_cte_trgm_operator_executes(
    session: AsyncSession,
) -> None:
    """The pg_trgm ``%`` seed predicate runs AND matches a near-duplicate name."""
    vault = Vault(id=uuid4(), name=f'seed-trgm-{uuid4().hex[:8]}')
    entity = Entity(id=uuid4(), canonical_name='mcp servers')
    session.add_all([vault, entity])
    await session.commit()

    seed_cte = build_seed_entity_cte(
        query='mcp server',  # near-duplicate of 'mcp servers' → trigram % match
        ner_model=None,
        similarity_threshold=0.3,
        include_ilike=True,
        enable_semantic_seeding=False,
    )
    rows = (await session.execute(select(seed_cte.c.id))).all()
    assert any(r[0] == entity.id for r in rows), 'pg_trgm % operator should match the near-dup name'


async def _neighbour_with_chunk(
    session: AsyncSession, vault_id, seed_id, *, name: str, cooc: int, mention: int
):
    """Create a neighbour entity co-occurring with the seed, reachable via a
    single chunk: Entity -> EntityCooccurrence(seed), and Chunk -> Note ->
    MemoryUnit -> UnitEntity(neighbour). Returns (neighbour_id, chunk_id)."""
    neighbour = Entity(id=uuid4(), canonical_name=name, mention_count=mention)
    session.add(neighbour)
    await session.commit()

    e1, e2 = sorted([seed_id, neighbour.id], key=str)
    session.add(
        EntityCooccurrence(
            entity_id_1=e1,
            entity_id_2=e2,
            vault_id=vault_id,
            cooccurrence_count=cooc,
            last_cooccurred=datetime.now(timezone.utc),
        )
    )
    note = Note(id=uuid4(), vault_id=vault_id, content_hash=uuid4().hex)
    session.add(note)
    await session.commit()
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault_id,
        note_id=note.id,
        text=f'unit-{uuid4().hex}',
        event_date=datetime.now(timezone.utc),
    )
    session.add(unit)
    await session.commit()
    chunk = Chunk(
        id=uuid4(),
        note_id=note.id,
        vault_id=vault_id,
        text=f'chunk-{uuid4().hex}',
        content_hash=uuid4().hex,
        status=ContentStatus.ACTIVE,
        embedding=[0.0] * EMBEDDING_DIMENSION,
        chunk_index=0,
    )
    session.add_all(
        [unit, chunk, UnitEntity(unit_id=unit.id, entity_id=neighbour.id, vault_id=vault_id)]
    )
    await session.commit()
    return neighbour.id, chunk.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_neighbour_cap_drops_weakest_below_cap(
    session: AsyncSession,
) -> None:
    """With max_neighbors=2 and three neighbours of distinct link_strength, the
    chunk reachable ONLY through the weakest (below-cap) neighbour is dropped,
    while the two strongest survive. Gates the cap's SELECTION, not just that it
    executes. link_strength = ln(sum_cooc+1)/ln(mention+2); equal mention, so
    strength ordering follows cooccurrence_count: strong(100) > mid(50) > weak(1).
    """
    vault = Vault(id=uuid4(), name=f'cap-select-{uuid4().hex[:8]}')
    seed = Entity(id=uuid4(), canonical_name=f'capseed{uuid4().hex[:8]}', mention_count=1)
    session.add_all([vault, seed])
    await session.commit()

    _, chunk_strong = await _neighbour_with_chunk(
        session, vault.id, seed.id, name=f'nbr-strong-{uuid4().hex[:6]}', cooc=100, mention=1
    )
    _, chunk_mid = await _neighbour_with_chunk(
        session, vault.id, seed.id, name=f'nbr-mid-{uuid4().hex[:6]}', cooc=50, mention=1
    )
    _, chunk_weak = await _neighbour_with_chunk(
        session, vault.id, seed.id, name=f'nbr-weak-{uuid4().hex[:6]}', cooc=1, mention=1
    )

    await session.exec(text('SELECT set_limit(0.3)'))
    strat = EntityCooccurrenceNoteGraphStrategy(max_neighbors=2, enable_semantic_seeding=False)
    # Query == seed canonical_name → fallback seed matching finds the seed.
    stmt = strat.get_statement(seed.canonical_name, None, limit=60, vault_ids=[vault.id])
    result = await session.execute(stmt)
    chunk_ids = {row[0] for row in result.all()}

    assert chunk_strong in chunk_ids, 'top-1 neighbour chunk must survive the cap'
    assert chunk_mid in chunk_ids, 'top-2 neighbour chunk must survive the cap'
    assert chunk_weak not in chunk_ids, 'below-cap (weakest) neighbour chunk must be dropped'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_statement_timeout_is_detected_by_helper(session: AsyncSession) -> None:
    """Close the loop on the fallback: a REAL Postgres statement_timeout surfaces
    as the DBAPIError that _is_statement_timeout recognizes. Combined with the
    mocked control-flow tests (timeout -> rollback -> retry cheap signals), this
    proves the end-to-end degradation path. SET LOCAL keeps the low timeout
    transaction-scoped so it can't leak to a pooled connection."""
    from sqlalchemy.exc import DBAPIError

    from memex_core.memory.retrieval.document_search import _is_statement_timeout

    await session.exec(text("SET LOCAL statement_timeout = '100ms'"))
    with pytest.raises(DBAPIError) as ei:
        await session.exec(text('SELECT pg_sleep(1)'))
    await session.rollback()
    assert _is_statement_timeout(ei.value), 'a real statement_timeout must be detected'
