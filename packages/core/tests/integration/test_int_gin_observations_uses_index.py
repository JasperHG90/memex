"""Integration tests pinning the GIN-index plan on ``mental_models.observations``.

Migration 044 added a ``GIN (observations jsonb_path_ops)`` index to back
the two production JSONB-containment scans:

  * ``_find_source_mus_for_observation`` — ``observations @> [{'id': ...}]``
    used to resolve a unit_id that turns out to be a read-only Observation.
  * ``flush_deferred_observation_refresh`` — ``mm.observations @> probe.p``
    in a LATERAL unnest over many probes; this is the deprio→refresh path
    that scales with the size of the deprio burst.

Both queries must hit ``idx_mental_models_observations_gin`` once the
table is large enough that the planner discards a Seq Scan. This test
seeds >100 mental_models with synthetic observations and asserts the
EXPLAIN plan contains an index scan referencing that index by name.

Without these gates, a future refactor that silently switched to a
non-containment predicate (or dropped the GIN ops class) would
cross-machine regress the deprio path into an O(N) JSONB scan.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import String as SAString

from memex_core.memory.sql_models import Entity, MentalModel, Vault

pytestmark = [pytest.mark.integration]


GIN_INDEX_NAME = 'idx_mental_models_observations_gin'


async def _seed_mental_models(session, *, count: int = 120) -> tuple:
    """Seed ``count`` mental_models in a fresh vault and entity, returning
    ``(vault_id, target_observation_id, target_mu_id)``.

    Every mental_model carries a single Observation with a unique uuid4
    id. The one whose index == count // 2 holds the target id; that
    Observation also lists ``target_mu_id`` in its evidence so both the
    ``@> [{'id': ...}]`` probe AND the ``@> [{'evidence': [{'memory_id': ...}]}]``
    probe match exactly one row.
    """
    vault_id = uuid4()
    target_obs_id = uuid4()
    target_mu_id = uuid4()

    session.add(Vault(id=vault_id, name=f'gin-{vault_id.hex[:8]}'))
    await session.flush()

    # One entity is sufficient for the index test — the unique-arbiter is
    # (entity_id, vault_id), so we use a fresh entity per mental_model to
    # keep all rows in the same vault while satisfying the unique
    # constraint.
    target_index = count // 2
    for i in range(count):
        entity_id = uuid4()
        session.add(Entity(id=entity_id, canonical_name=f'gin-entity-{entity_id.hex[:8]}'))

        if i == target_index:
            obs_id = target_obs_id
            evidence_mu = target_mu_id
        else:
            obs_id = uuid4()
            evidence_mu = uuid4()

        mm = MentalModel(
            id=uuid4(),
            entity_id=entity_id,
            vault_id=vault_id,
            name=f'mm-{i}',
            version=1,
            observations=[
                {
                    'id': str(obs_id),
                    'title': f'obs-{i}',
                    'content': f'content-{i}',
                    'trend': 'new',
                    'evidence': [{'memory_id': str(evidence_mu), 'quote': None, 'relevance': 1.0}],
                }
            ],
            embedding=[0.1] * 384,
            last_refreshed=datetime.now(timezone.utc),
        )
        session.add(mm)

    await session.commit()
    # Ensure the planner has up-to-date statistics so it prefers the GIN
    # index over a Seq Scan on this small-but-realistic dataset.
    await session.execute(text('ANALYZE mental_models'))
    await session.commit()
    return vault_id, target_obs_id, target_mu_id


def _plan_text(rows) -> str:
    """Join EXPLAIN result rows into a single text blob for substring asserts."""
    return '\n'.join(str(r[0]) for r in rows)


def _assert_gin_index_used(plan: str) -> None:
    """The plan must reference ``GIN_INDEX_NAME`` under an index-scan node.

    Postgres' GIN access path is exposed as ``Bitmap Index Scan`` (the
    most common) or, on very small selectivity, ``Index Scan``. We accept
    either, but we MUST NOT see ``Seq Scan on mental_models`` as the
    primary access path — that would silently regress the deprio scan
    into O(rows × json_depth).
    """
    has_bitmap = 'Bitmap Index Scan' in plan
    has_index = 'Index Scan' in plan
    refs_index = GIN_INDEX_NAME in plan
    assert refs_index, f'expected plan to reference {GIN_INDEX_NAME}; got plan:\n{plan}'
    assert has_bitmap or has_index, (
        f'expected Bitmap Index Scan or Index Scan in plan; got:\n{plan}'
    )
    # Sanity: a Seq Scan on mental_models is the regression we are gating.
    assert 'Seq Scan on mental_models' not in plan, (
        f'plan fell back to Seq Scan on mental_models — GIN index not used:\n{plan}'
    )


@pytest.mark.asyncio
async def test_find_source_mus_for_observation_uses_gin(metastore):
    """``_find_source_mus_for_observation`` JSONB containment must hit GIN."""
    async with metastore.session() as session:
        vault_id, target_obs_id, _ = await _seed_mental_models(session)

    probe = json.dumps([{'id': str(target_obs_id)}])

    # Production query shape from
    # ``UnitsService._find_source_mus_for_observation`` — SELECT the
    # observations column under a vault-scoped @> containment with
    # deterministic ordering.
    sql = (
        'EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) '
        'SELECT observations FROM mental_models '
        'WHERE observations @> CAST(:probe AS jsonb) '
        'AND vault_id = :vault_id '
        'ORDER BY id'
    )

    async with metastore.session() as session:
        result = await session.execute(text(sql), {'probe': probe, 'vault_id': vault_id})
        plan = _plan_text(result.all())

    _assert_gin_index_used(plan)


@pytest.mark.asyncio
async def test_flush_deferred_observation_refresh_uses_gin(metastore):
    """The LATERAL probe form used by ``flush_deferred_observation_refresh`` must hit GIN."""
    async with metastore.session() as session:
        vault_id, _, target_mu_id = await _seed_mental_models(session)

    # Production query shape from
    # ``UnitsService.flush_deferred_observation_refresh`` — LATERAL
    # unnest(:probes::jsonb[]) joined against ``mm.observations @> probe.p``,
    # vault-scoped. We drop ``FOR UPDATE OF mm SKIP LOCKED`` so EXPLAIN
    # ANALYZE doesn't require holding a write lock; the WHERE clause that
    # selects the index path is unchanged.
    probes_json = [
        json.dumps([{'evidence': [{'memory_id': str(target_mu_id)}]}]),
        # A second, non-matching probe to mirror real usage where the
        # batch contains multiple deprio'd MU ids per flush call.
        json.dumps([{'evidence': [{'memory_id': str(uuid4())}]}]),
    ]

    sql = (
        'EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) '
        'SELECT mm.id, mm.entity_id, mm.observations '
        'FROM mental_models mm, '
        'LATERAL unnest(CAST(:probes AS jsonb[])) AS probe(p) '
        'WHERE mm.vault_id = :vault_id AND mm.observations @> probe.p'
    )
    stmt = text(sql).bindparams(bindparam('probes', type_=ARRAY(SAString)))

    async with metastore.session() as session:
        result = await session.execute(stmt, {'vault_id': vault_id, 'probes': probes_json})
        plan = _plan_text(result.all())

    _assert_gin_index_used(plan)


# ---------------------------------------------------------------------------
# Behavioural regressions for the JSONB-codec double-encode bug.
#
# The two EXPLAIN tests above reconstruct the containment query as RAW SQL
# (``CAST(:probe AS jsonb)``), which binds the json.dumps string through
# asyncpg's TEXT protocol and lands a native array — so they validate the
# index plan but NOT the SQLAlchemy expression the production code builds.
#
# ``UnitsService`` builds the predicate with the SQLAlchemy ``cast()``
# construct. A bare ``cast(probe, JSONB)`` (probe = json.dumps(...)) makes
# SQLAlchemy bind ``probe`` through asyncpg's JSONB codec, double-encoding the
# already-JSON string into a JSONB *string scalar* — so ``@>`` matches NOTHING
# and the method silently returns no rows. These tests call the REAL methods
# and assert a match, so the bug (and any regression to it) fails loudly.
# ---------------------------------------------------------------------------


def _units_service(metastore, filestore, memex_config):
    from memex_core.services.units import UnitsService

    return UnitsService(metastore, filestore, memex_config)


@pytest.mark.asyncio
async def test_find_source_mus_for_observation_matches_via_orm(metastore, filestore, memex_config):
    """The REAL ``_find_source_mus_for_observation`` must resolve the seeded
    observation id to its evidence MU (would return ``None`` under the
    double-encode bug)."""
    async with metastore.session() as session:
        vault_id, target_obs_id, target_mu_id = await _seed_mental_models(session, count=12)

    service = _units_service(metastore, filestore, memex_config)
    async with metastore.session() as session:
        result = await service._find_source_mus_for_observation(
            session, target_obs_id, vault_id=vault_id
        )

    assert result is not None, (
        'observation @> probe matched nothing — JSONB double-encode regression'
    )
    assert target_mu_id in result

    # A genuinely-absent observation id must still return None (no false match).
    async with metastore.session() as session:
        miss = await service._find_source_mus_for_observation(session, uuid4(), vault_id=vault_id)
    assert miss is None


@pytest.mark.asyncio
async def test_enqueue_refresh_tasks_for_mu_finds_citing_observation(
    metastore, filestore, memex_config
):
    """The REAL ``_enqueue_refresh_tasks_for_mu`` must find the MentalModel whose
    observation cites the deprio'd MU and enqueue a ``refresh_observation`` row
    (enqueues NOTHING under the double-encode bug -> silent staleness)."""
    from memex_core.memory.sql_models import ReflectionQueue
    from sqlmodel import col, select as sm_select

    async with metastore.session() as session:
        vault_id, _, target_mu_id = await _seed_mental_models(session, count=12)

    service = _units_service(metastore, filestore, memex_config)
    async with metastore.session() as session:
        await service._enqueue_refresh_tasks_for_mu(
            session, triggering_unit_id=target_mu_id, vault_id=vault_id
        )
        await session.commit()

    async with metastore.session() as session:
        rows = (
            await session.execute(
                sm_select(ReflectionQueue).where(
                    col(ReflectionQueue.vault_id) == vault_id,
                    col(ReflectionQueue.source_unit_id) == target_mu_id,
                    col(ReflectionQueue.task_type) == 'refresh_observation',
                )
            )
        ).all()

    assert len(rows) == 1, (
        f'expected exactly one refresh_observation enqueued, got {len(rows)} — '
        'JSONB double-encode regression (observations @> probe matched nothing)'
    )
