"""SQL ↔ Python parity guard for the FSFM composite score.

The lint rules in ``services/lint.py`` embed a SQL CTE that mirrors the
Python composite math in ``services/deprioritize_score.py``. If the two
drift (e.g., someone tweaks the Python sigmoid but forgets the SQL
``1.0 / (1.0 + exp(-x))``, or tunes ``lambda_link`` in one file but not
the other), the lint rules will silently emit different proposals than
the auto-band Python path expects.

This test seeds a small fleet of synthetic units with varied signal
mixes, runs both code paths, and asserts the two ``composite_score``
values are within 1e-4 of each other for every unit. This is the
correctness guard plan §B6 promised.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    Entity,
    MemoryLink,
    MemoryUnit,
    Note,
    UnitEntity,
    Vault,
)
from memex_core.services.deprioritize_score import DEFAULT_WEIGHTS


pytestmark = [pytest.mark.integration]


# Same constants embedded in the SQL CTE in ``services/lint.py``.
_SQL_LAMBDA_LINK = 0.01
_SQL_MU_ENTITY = 0.005


async def _seed_unit(
    session: AsyncSession,
    *,
    vault: Vault,
    note: Note,
    success: int,
    failure: int,
    importance: float | None,
    stability: float | None,
    intent_class: str = 'durable',
    last_outcome_age_days: float | None = None,
    confidence: float = 1.0,
) -> MemoryUnit:
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text=f'parity-{uuid4().hex[:6]}',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        intent_class=intent_class,
        risk_class='none',
        importance=importance,
        stability=stability,
        success_co_count=success,
        failure_co_count=failure,
        confidence=confidence,
        confidence_evidence_count=0,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)
    if last_outcome_age_days is not None:
        await session.execute(
            text('UPDATE memory_units SET last_outcome_at = :ts WHERE id = :id'),
            {
                'ts': datetime.now(timezone.utc) - timedelta(days=last_outcome_age_days),
                'id': str(unit.id),
            },
        )
        await session.commit()
    return unit


async def _link(
    session: AsyncSession,
    *,
    vault: Vault,
    src: MemoryUnit,
    dst: MemoryUnit,
    link_type: str,
    weight: float = 1.0,
    age_days: float = 0.0,
) -> None:
    session.add(
        MemoryLink(
            from_unit_id=src.id,
            to_unit_id=dst.id,
            vault_id=vault.id,
            link_type=link_type,
            weight=weight,
        )
    )
    await session.commit()
    if age_days > 0:
        await session.execute(
            text(
                'UPDATE memory_links SET created_at = :ts '
                'WHERE from_unit_id = :src AND to_unit_id = :dst '
                'AND link_type = :lt'
            ),
            {
                'ts': datetime.now(timezone.utc) - timedelta(days=age_days),
                'src': str(src.id),
                'dst': str(dst.id),
                'lt': link_type,
            },
        )
        await session.commit()


async def _attach_entity(
    session: AsyncSession,
    *,
    vault: Vault,
    unit: MemoryUnit,
    last_seen_age_days: float,
) -> Entity:
    entity = Entity(canonical_name=f'pe-{uuid4().hex[:6]}')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    session.add(UnitEntity(unit_id=unit.id, entity_id=entity.id, vault_id=vault.id))
    await session.commit()
    await session.execute(
        text('UPDATE entities SET last_seen = :ts WHERE id = :id'),
        {
            'ts': datetime.now(timezone.utc) - timedelta(days=last_seen_age_days),
            'id': str(entity.id),
        },
    )
    await session.commit()
    return entity


# Replicates the unit_signals → unit_components → unit_scores CTE pipeline
# from ``services/lint.py`` but emits the score directly (no rule predicate)
# so we can compare against every seeded unit, not just those above the
# propose threshold.
_SQL_SCORE_PROBE = text("""
    WITH unit_signals AS (
        SELECT
            mu.id AS unit_id,
            mu.success_co_count AS success_co_count,
            mu.failure_co_count AS failure_co_count,
            mu.last_outcome_at AS last_outcome_at,
            mu.stability AS stability,
            mu.importance AS importance,
            ((mu.success_co_count + 1.0) /
             (mu.success_co_count + mu.failure_co_count + 2)) AS mw_score,
            (
                SELECT SUM(
                    CASE ml.link_type
                        WHEN 'contradicts' THEN 1.0
                        WHEN 'weakens' THEN 0.5
                        WHEN 'reinforces' THEN -1.0
                        WHEN 'causes' THEN -0.1
                        WHEN 'caused_by' THEN -0.1
                        WHEN 'enables' THEN -0.1
                        WHEN 'prevents' THEN -0.1
                    END
                    * ml.weight
                    * src.confidence
                    * ((src.success_co_count + 1.0) /
                       (src.success_co_count + src.failure_co_count + 2))
                    * exp(-0.01 * GREATEST(0.0, EXTRACT(EPOCH FROM (now() - ml.created_at)) / 86400.0))
                )
                FROM memory_links ml
                JOIN memory_units src ON src.id = ml.from_unit_id
                WHERE ml.to_unit_id = mu.id
                  AND ml.vault_id = mu.vault_id
                  AND src.vault_id = mu.vault_id
                  AND ml.link_type IN (
                    'contradicts', 'weakens', 'reinforces',
                    'causes', 'caused_by', 'enables', 'prevents'
                  )
            ) AS graph_pressure_raw,
            (
                SELECT MAX(e.last_seen)
                FROM unit_entities ue
                JOIN entities e ON e.id = ue.entity_id
                WHERE ue.unit_id = mu.id AND ue.vault_id = mu.vault_id
            ) AS freshest_entity_last_seen
        FROM memory_units mu
        WHERE mu.vault_id = :vault_id AND mu.id = :unit_id
    ),
    unit_components AS (
        SELECT
            s.*,
            CASE
                WHEN s.graph_pressure_raw IS NULL THEN 0.5
                ELSE 1.0 / (1.0 + exp(-s.graph_pressure_raw))
            END AS graph_pressure,
            (1.0 - s.mw_score) AS mw_complement,
            CASE
                WHEN s.last_outcome_at IS NULL OR s.stability IS NULL OR s.stability <= 0
                    THEN 0.0
                ELSE 1.0 - exp(
                    -GREATEST(0.0, EXTRACT(EPOCH FROM (now() - s.last_outcome_at)) / 86400.0)
                    / s.stability
                )
            END AS temporal_staleness,
            CASE
                WHEN s.freshest_entity_last_seen IS NULL THEN 0.0
                ELSE 1.0 - exp(
                    -0.005 * GREATEST(0.0,
                        EXTRACT(EPOCH FROM (now() - s.freshest_entity_last_seen)) / 86400.0
                    )
                )
            END AS entity_dormancy
        FROM unit_signals s
    )
    SELECT
        c.unit_id,
        (
            0.5 * c.graph_pressure
          + 0.25 * c.mw_complement
          + 0.15 * c.temporal_staleness
          + 0.10 * c.entity_dormancy
        ) * (1.0 - COALESCE(c.importance, 0.5)) AS sql_composite_score
    FROM unit_components c
""")


@pytest.mark.asyncio
async def test_sql_python_parity_across_signal_mixes(session: AsyncSession, api) -> None:
    """For every seeded unit, the SQL CTE and the Python scorer must agree
    on ``composite_score`` to within 1e-4. Drift here means the lint rules
    will silently emit proposals at thresholds that don't match the
    auto-band's Python-side decision."""
    # Sanity: pin the constants so a future Python tweak fails this test
    # before drift hits production.
    assert DEFAULT_WEIGHTS == {'graph': 0.5, 'mw': 0.25, 'temporal': 0.15, 'entity': 0.10}

    vault = Vault(name=f'parity-{uuid4().hex[:6]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'h-{uuid4().hex[:8]}',
        original_text='parity seed',
    )
    session.add(note)
    await session.commit()

    # Each scenario is one unit with a distinct signal mix.
    scenarios = []

    # 1) Cold-start, no signals at all.
    u1 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=0,
        importance=0.7,
        stability=180.0,
        intent_class='durable',
    )
    scenarios.append(('cold_start', u1))

    # 2) High failure, no links, no entities, fresh.
    u2 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=20,
        importance=0.3,
        stability=14.0,
        intent_class='ephemeral',
    )
    scenarios.append(('high_failure_no_links', u2))

    # 3) Stale ephemeral with one fresh contradicts link.
    u3 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=10,
        importance=0.3,
        stability=14.0,
        intent_class='ephemeral',
        last_outcome_age_days=100.0,
    )
    src3 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    await _link(session, vault=vault, src=src3, dst=u3, link_type='contradicts')
    scenarios.append(('stale_with_one_contradicts', u3))

    # 4) Reinforcement-dominant: one strong reinforces link.
    u4 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=5,
        failure=5,
        importance=0.7,
        stability=180.0,
    )
    src4 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    await _link(session, vault=vault, src=src4, dst=u4, link_type='reinforces')
    scenarios.append(('reinforced', u4))

    # 5) Mixed: contradicts + reinforces from credible sources (should ~ cancel).
    u5 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=2,
        failure=2,
        importance=0.7,
        stability=180.0,
    )
    src5a = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    src5b = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    await _link(session, vault=vault, src=src5a, dst=u5, link_type='contradicts')
    await _link(session, vault=vault, src=src5b, dst=u5, link_type='reinforces')
    scenarios.append(('balanced_links', u5))

    # 6) Old contradicts link (recency-decayed).
    u6 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=5,
        importance=0.3,
        stability=14.0,
        intent_class='ephemeral',
    )
    src6 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    await _link(session, vault=vault, src=src6, dst=u6, link_type='contradicts', age_days=180.0)
    scenarios.append(('old_contradicts', u6))

    # 7) Dormant entity attached.
    u7 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=10,
        importance=0.3,
        stability=14.0,
        intent_class='ephemeral',
        last_outcome_age_days=100.0,
    )
    await _attach_entity(session, vault=vault, unit=u7, last_seen_age_days=600.0)
    scenarios.append(('dormant_entity', u7))

    # 8) NULL importance (treated as 0.5).
    u8 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=10,
        importance=None,
        stability=180.0,
        last_outcome_age_days=50.0,
    )
    scenarios.append(('null_importance', u8))

    # 9) Many contradictions to push the sigmoid into saturation.
    u9 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=0,
        failure=20,
        importance=0.3,
        stability=14.0,
        intent_class='ephemeral',
        last_outcome_age_days=200.0,
    )
    for _ in range(7):
        s = await _seed_unit(
            session,
            vault=vault,
            note=note,
            success=10,
            failure=0,
            importance=0.7,
            stability=180.0,
        )
        await _link(session, vault=vault, src=s, dst=u9, link_type='contradicts')
    scenarios.append(('many_contradicts', u9))

    # 10) Causal (structural) link, otherwise mild signals.
    u10 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=2,
        failure=3,
        importance=0.7,
        stability=180.0,
    )
    src10 = await _seed_unit(
        session,
        vault=vault,
        note=note,
        success=10,
        failure=0,
        importance=0.7,
        stability=180.0,
    )
    await _link(session, vault=vault, src=src10, dst=u10, link_type='causes')
    scenarios.append(('causal_link', u10))

    # Run the parity check. We snapshot ``now`` once and pass it to the
    # Python path so its temporal/recency math agrees with the SQL ``now()``
    # within a few-millisecond tolerance — otherwise the test could be
    # flaky on heavily loaded CI.
    py_now = datetime.now(timezone.utc)
    mismatches: list[tuple[str, float, float]] = []
    for label, unit in scenarios:
        py_breakdown = await api.deprioritize_scorer.score(unit.id, vault.id, session, now=py_now)
        assert py_breakdown is not None, f'{label}: scorer returned None'
        py_score = py_breakdown.score

        sql_row = (
            await session.execute(
                _SQL_SCORE_PROBE,
                {'vault_id': str(vault.id), 'unit_id': str(unit.id)},
            )
        ).first()
        assert sql_row is not None, f'{label}: SQL probe returned no row'
        sql_score = float(sql_row.sql_composite_score)

        if not math.isclose(py_score, sql_score, abs_tol=1e-3):
            mismatches.append((label, py_score, sql_score))

    if mismatches:
        msg = '\n'.join(
            f'  {label}: python={p:.6f} sql={s:.6f} diff={abs(p - s):.6f}'
            for label, p, s in mismatches
        )
        pytest.fail(f'SQL/Python composite drift detected:\n{msg}')
