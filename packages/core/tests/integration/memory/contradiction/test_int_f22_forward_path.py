"""F22 — contradiction-engine forward-path bumps confidence_evidence_count.

Drives weaken / contradict / reinforce events through the contradiction
engine with a stubbed ``_classify`` so the test asserts the bookkeeping
without standing up a real LLM.

Forward-path symmetry: ``confidence_evidence_count`` MUST be bumped on the
weaken / contradict α-steps and MUST NOT be bumped on reinforce — same
events that adjust ``confidence`` itself.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import ContradictionConfig, GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.contradiction.signatures import ContradictionRelationship
from memex_core.memory.sql_models import MemoryUnit, Note


@pytest.fixture
def contradiction_config() -> ContradictionConfig:
    return ContradictionConfig(
        enabled=True,
        alpha=0.1,
        similarity_threshold=0.5,
        max_candidates_per_unit=15,
        superseded_threshold=0.3,
    )


def _make_note(vault_id, title: str = 'F22 Test Note') -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title=title,
        content_hash=str(uuid4()),
        original_text=f'F22 test {uuid4()}',
    )


def _make_unit(note_id, vault_id, text: str, confidence: float = 1.0) -> MemoryUnit:
    return MemoryUnit(
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=confidence,
        confidence_evidence_count=0,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )


async def _seed_unit_pair(
    session: AsyncSession,
) -> tuple[MemoryUnit, MemoryUnit]:
    vault_id = GLOBAL_VAULT_ID
    note_old = _make_note(vault_id, 'old')
    note_new = _make_note(vault_id, 'new')
    session.add_all([note_old, note_new])
    await session.flush()

    old_unit = _make_unit(note_old.id, vault_id, f'Old fact {uuid4()}', confidence=1.0)
    new_unit = _make_unit(note_new.id, vault_id, f'Corrective fact {uuid4()}', confidence=1.0)
    old_unit.event_date = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_unit.event_date = datetime(2025, 6, 1, tzinfo=timezone.utc)
    session.add_all([old_unit, new_unit])
    await session.commit()
    return old_unit, new_unit


def _stub_classify(relation: str, existing_id: str) -> AsyncMock:
    rel = ContradictionRelationship(
        existing_id=existing_id,
        relation=relation,
        authoritative='new',
        reasoning=f'F22 test: {relation}',
    )
    return AsyncMock(return_value=[rel])


def _stub_get_candidates(candidates: list[MemoryUnit]) -> AsyncMock:
    return AsyncMock(return_value=candidates)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weaken_bumps_evidence_count_and_decreases_confidence(
    session: AsyncSession, contradiction_config: ContradictionConfig
) -> None:
    """A weaken event bumps count by 1 AND decreases confidence by α."""
    old_unit, new_unit = await _seed_unit_pair(session)
    engine = ContradictionEngine(lm=MagicMock(), config=contradiction_config)

    with patch(
        'memex_core.memory.contradiction.engine.get_candidates',
        _stub_get_candidates([old_unit]),
    ):
        with patch.object(engine, '_triage', AsyncMock(return_value=[str(new_unit.id)])):
            with patch.object(engine, '_classify', _stub_classify('weaken', str(old_unit.id))):
                await engine._detect(session, [new_unit.id], GLOBAL_VAULT_ID)
    await session.commit()

    refreshed = (await session.exec(select(MemoryUnit).where(MemoryUnit.id == old_unit.id))).first()
    assert refreshed is not None
    assert refreshed.confidence_evidence_count == 1
    assert math.isclose(refreshed.confidence, 0.9, rel_tol=1e-6)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contradict_bumps_evidence_count_and_decreases_confidence(
    session: AsyncSession, contradiction_config: ContradictionConfig
) -> None:
    """A contradict event bumps count by 1 AND decreases confidence by 2α."""
    old_unit, new_unit = await _seed_unit_pair(session)
    engine = ContradictionEngine(lm=MagicMock(), config=contradiction_config)

    with patch(
        'memex_core.memory.contradiction.engine.get_candidates',
        _stub_get_candidates([old_unit]),
    ):
        with patch.object(engine, '_triage', AsyncMock(return_value=[str(new_unit.id)])):
            with patch.object(engine, '_classify', _stub_classify('contradict', str(old_unit.id))):
                await engine._detect(session, [new_unit.id], GLOBAL_VAULT_ID)
    await session.commit()

    refreshed = (await session.exec(select(MemoryUnit).where(MemoryUnit.id == old_unit.id))).first()
    assert refreshed is not None
    assert refreshed.confidence_evidence_count == 1
    assert math.isclose(refreshed.confidence, 0.8, rel_tol=1e-6)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reinforce_does_not_bump_evidence_count(
    session: AsyncSession, contradiction_config: ContradictionConfig
) -> None:
    """A reinforce event does NOT bump count (preserves backfill symmetry).

    Reinforcement only nudges confidence upward — it does NOT touch
    confidence_evidence_count. This is the load-bearing symmetry with the
    backfill query (which excludes reinforces from the count).
    """
    old_unit, new_unit = await _seed_unit_pair(session)
    # Start old_unit at 0.7 so reinforcement has room to grow
    old_unit.confidence = 0.7
    session.add(old_unit)
    await session.commit()

    engine = ContradictionEngine(lm=MagicMock(), config=contradiction_config)

    with patch(
        'memex_core.memory.contradiction.engine.get_candidates',
        _stub_get_candidates([old_unit]),
    ):
        with patch.object(engine, '_triage', AsyncMock(return_value=[str(new_unit.id)])):
            with patch.object(engine, '_classify', _stub_classify('reinforce', str(old_unit.id))):
                await engine._detect(session, [new_unit.id], GLOBAL_VAULT_ID)
    await session.commit()

    refreshed_old = (
        await session.exec(select(MemoryUnit).where(MemoryUnit.id == old_unit.id))
    ).first()
    refreshed_new = (
        await session.exec(select(MemoryUnit).where(MemoryUnit.id == new_unit.id))
    ).first()
    assert refreshed_old is not None and refreshed_new is not None
    # Both units' confidence steps up by α; count is UNCHANGED on both.
    assert refreshed_old.confidence_evidence_count == 0
    assert refreshed_new.confidence_evidence_count == 0
    assert math.isclose(refreshed_old.confidence, 0.8, rel_tol=1e-6)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_count_accumulates_across_events(
    session: AsyncSession, contradiction_config: ContradictionConfig
) -> None:
    """Two consecutive weaken events on the same unit bring count to 2."""
    old_unit, new_unit = await _seed_unit_pair(session)
    engine = ContradictionEngine(lm=MagicMock(), config=contradiction_config)

    for _ in range(2):
        with patch(
            'memex_core.memory.contradiction.engine.get_candidates',
            _stub_get_candidates([old_unit]),
        ):
            with patch.object(engine, '_triage', AsyncMock(return_value=[str(new_unit.id)])):
                with patch.object(engine, '_classify', _stub_classify('weaken', str(old_unit.id))):
                    await engine._detect(session, [new_unit.id], GLOBAL_VAULT_ID)
        await session.commit()
        await session.refresh(old_unit)

    refreshed = (await session.exec(select(MemoryUnit).where(MemoryUnit.id == old_unit.id))).first()
    assert refreshed is not None
    assert refreshed.confidence_evidence_count == 2
    # confidence: 1.0 → 0.9 → 0.8
    assert math.isclose(refreshed.confidence, 0.8, rel_tol=1e-6)
