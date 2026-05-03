"""F22 — end-to-end confidence-composition integration tests.

Verifies:

- The migration backfill counts contradicts/weakens links only — reinforces
  excluded for symmetry with the forward path.
- With ``certainty_modulation_enabled = False`` (ship default) the F47 boost
  is bit-for-bit identical to the pre-F22 baseline (regression guard).
- With the flag flipped to True, the boost shape changes per the BACKLOG
  worked cases.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.confidence import certainty
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryLink, MemoryUnit, Note
from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED


def _make_note(vault_id, title: str = 'F22 test note') -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title=title,
        content_hash=str(uuid4()),
        original_text=f'F22 {uuid4()}',
    )


def _make_unit(
    note_id,
    vault_id,
    text: str,
    confidence: float = 1.0,
    confidence_evidence_count: int = 0,
) -> MemoryUnit:
    return MemoryUnit(
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=confidence,
        confidence_evidence_count=confidence_evidence_count,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )


def _make_link(from_unit_id, to_unit_id, link_type: str, vault_id) -> MemoryLink:
    return MemoryLink(
        from_unit_id=from_unit_id,
        to_unit_id=to_unit_id,
        link_type=link_type,
        vault_id=vault_id,
        weight=1.0,
        link_metadata={},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_counts_contradicts_and_weakens_only(session: AsyncSession) -> None:
    """Migration backfill: contradicts + weakens counted; reinforces excluded.

    Three pre-existing units:
      (a) 5 incoming contradicts/weakens, 0 reinforces → count = 5
      (b) 0 contradicts/weakens, 10 reinforces → count = 0 (the v1 limitation)
      (c) no incoming links → count = 0
    """
    vault_id = GLOBAL_VAULT_ID
    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    target_a = _make_unit(note.id, vault_id, f'target a {uuid4()}')
    target_b = _make_unit(note.id, vault_id, f'target b {uuid4()}')
    target_c = _make_unit(note.id, vault_id, f'target c {uuid4()}')
    session.add_all([target_a, target_b, target_c])
    await session.flush()

    sources = [_make_unit(note.id, vault_id, f'src {i} {uuid4()}') for i in range(15)]
    session.add_all(sources)
    await session.flush()

    # 5 contradicts/weakens → target_a (mix of types)
    for i in range(5):
        link_type = 'contradicts' if i % 2 == 0 else 'weakens'
        session.add(_make_link(sources[i].id, target_a.id, link_type, vault_id))
    # 10 reinforces → target_b
    for i in range(5, 15):
        session.add(_make_link(sources[i].id, target_b.id, 'reinforces', vault_id))
    # target_c: no incoming links

    # Reset to 0 to simulate pre-F22 state, then run the backfill SQL inline.
    target_a.confidence_evidence_count = 0
    target_b.confidence_evidence_count = 0
    target_c.confidence_evidence_count = 0
    session.add_all([target_a, target_b, target_c])
    await session.commit()

    # Replay the migration backfill query.
    from sqlalchemy import text

    await session.execute(
        text(
            'UPDATE memory_units mu '
            'SET confidence_evidence_count = sub.cnt '
            'FROM ( '
            '    SELECT to_unit_id AS unit_id, COUNT(*) AS cnt '
            '    FROM memory_links '
            "    WHERE link_type IN ('contradicts', 'weakens') "
            '    GROUP BY to_unit_id '
            ') sub '
            'WHERE mu.id = sub.unit_id'
        )
    )
    await session.commit()

    target_a_id = target_a.id
    target_b_id = target_b.id
    target_c_id = target_c.id

    rows = (
        await session.execute(
            text('SELECT id, confidence_evidence_count FROM memory_units WHERE id = ANY(:ids)'),
            {'ids': [str(target_a_id), str(target_b_id), str(target_c_id)]},
        )
    ).all()
    counts = {row[0]: row[1] for row in rows}
    assert counts[target_a_id] == 5
    assert counts[target_b_id] == 0
    assert counts[target_c_id] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ship_default_boost_bit_for_bit_with_f47_baseline(
    session: AsyncSession,
) -> None:
    """certainty_modulation_enabled = False → F47 boost UNCHANGED across evidence_count values.

    Two units with identical confidence but different evidence counts; under
    the ship default they MUST produce identical boost factors. Under F22's
    flipped flag the same units would produce divergent boosts.
    """
    vault_id = GLOBAL_VAULT_ID
    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit_low_evidence = _make_unit(
        note.id, vault_id, 'low ev', confidence=0.85, confidence_evidence_count=0
    )
    unit_high_evidence = _make_unit(
        note.id, vault_id, 'high ev', confidence=0.85, confidence_evidence_count=20
    )
    session.add_all([unit_low_evidence, unit_high_evidence])
    await session.commit()

    config = RetrievalConfig(
        reranking_recency_alpha=0.0,
        reranking_temporal_alpha=0.0,
        reranking_mw_alpha=0.0,
        confidence_alpha=0.3,
        certainty_modulation_enabled=False,  # SHIP DEFAULT
    )
    reranker = MagicMock()
    reranker.score.return_value = [0.0]
    engine = RetrievalEngine(embedder=MagicMock(), reranker=reranker, retrieval_config=config)

    sum_before_a = CONFIDENCE_BOOST_OBSERVED._sum.get()
    await engine._rerank_results('q', [unit_low_evidence])
    boost_a = CONFIDENCE_BOOST_OBSERVED._sum.get() - sum_before_a

    sum_before_b = CONFIDENCE_BOOST_OBSERVED._sum.get()
    await engine._rerank_results('q', [unit_high_evidence])
    boost_b = CONFIDENCE_BOOST_OBSERVED._sum.get() - sum_before_b

    expected = 1.0 + 0.3 * (0.85 - 0.5)
    assert math.isclose(boost_a, expected, rel_tol=1e-6)
    assert math.isclose(boost_b, expected, rel_tol=1e-6)
    assert math.isclose(boost_a, boost_b, rel_tol=1e-9)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_on_modulates_boost_per_evidence_count(session: AsyncSession) -> None:
    """certainty_modulation_enabled = True → cold-start neutral, well-evidenced full lift.

    Three units at confidence=1.0 with evidence_count = (0, 1, 20):
      - count=0 → boost = 1.0 (cold-start neutral)
      - count=1 → mild lift (certainty ≈ 0.33)
      - count=20 → near-full lift (certainty ≈ 0.977)
    """
    vault_id = GLOBAL_VAULT_ID
    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit_cold = _make_unit(note.id, vault_id, 'cold', confidence=1.0, confidence_evidence_count=0)
    unit_one = _make_unit(
        note.id, vault_id, 'single obs', confidence=1.0, confidence_evidence_count=1
    )
    unit_well = _make_unit(
        note.id, vault_id, 'well evidenced', confidence=1.0, confidence_evidence_count=20
    )
    session.add_all([unit_cold, unit_one, unit_well])
    await session.commit()

    config = RetrievalConfig(
        reranking_recency_alpha=0.0,
        reranking_temporal_alpha=0.0,
        reranking_mw_alpha=0.0,
        confidence_alpha=0.3,
        certainty_modulation_enabled=True,
    )

    boosts: list[float] = []
    for unit in (unit_cold, unit_one, unit_well):
        reranker = MagicMock()
        reranker.score.return_value = [0.0]
        engine = RetrievalEngine(embedder=MagicMock(), reranker=reranker, retrieval_config=config)
        sum_before = CONFIDENCE_BOOST_OBSERVED._sum.get()
        await engine._rerank_results('q', [unit])
        boosts.append(CONFIDENCE_BOOST_OBSERVED._sum.get() - sum_before)

    boost_cold, boost_one, boost_well = boosts
    assert math.isclose(boost_cold, 1.0, rel_tol=1e-6)
    expected_one = 1.0 + 0.3 * 0.5 * certainty(1.0, 1)
    expected_well = 1.0 + 0.3 * 0.5 * certainty(1.0, 20)
    assert math.isclose(boost_one, expected_one, rel_tol=1e-6)
    assert math.isclose(boost_well, expected_well, rel_tol=1e-6)
    assert boost_cold < boost_one < boost_well


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_constraint_rejects_negative_evidence_count(
    session: AsyncSession,
) -> None:
    """Schema CHECK: confidence_evidence_count >= 0 — rejected at the database."""
    from sqlalchemy.exc import IntegrityError

    vault_id = GLOBAL_VAULT_ID
    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    bad_unit = _make_unit(note.id, vault_id, 'bad')
    bad_unit.confidence_evidence_count = -1
    session.add(bad_unit)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
