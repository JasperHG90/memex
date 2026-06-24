"""Runtime validation that the perf-fixed note-graph SQL executes on real Postgres.

Compile-level tests pin the SHAPE (pg_trgm ``%`` operator, neighbour LIMIT); this
pins that the generated SQL actually RUNS against Postgres — the ``%`` operator
requires the ``pg_trgm`` extension, and the capped ``UNION ALL ... ORDER BY ...
LIMIT`` neighbour CTE must be valid SQL. Executing without error is the gate.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig
from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceNoteGraphStrategy,
    build_seed_entity_cte,
)
from memex_core.memory.sql_models import Entity, Vault


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
