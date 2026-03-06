"""Integration tests for contradiction detection.

Tests marked @pytest.mark.integration use real DB (testcontainers) but mock LLM calls.
Tests marked @pytest.mark.llm use real DB AND real LLM calls (require GOOGLE_API_KEY).
"""

import os
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import dspy
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.contradiction.candidates import get_candidates
from memex_core.memory.sql_models import (
    MemoryUnit,
    MemoryLink,
    UnitEntity,
    Entity,
    Note,
)
from memex_common.config import ContradictionConfig, GLOBAL_VAULT_ID
from memex_common.types import FactTypes


@pytest.fixture
def contradiction_config() -> ContradictionConfig:
    return ContradictionConfig(
        enabled=True,
        alpha=0.1,
        similarity_threshold=0.5,
        max_candidates_per_unit=15,
        superseded_threshold=0.3,
    )


def _make_note(vault_id, title: str = 'Test Note') -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title=title,
        content_hash=str(uuid4()),
        original_text=f'Test content {uuid4()}',
    )


def _make_unit(
    note_id,
    vault_id,
    text: str,
    confidence: float = 1.0,
    event_date: datetime | None = None,
    embedding: list[float] | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=confidence,
        event_date=event_date or datetime.now(timezone.utc),
        embedding=embedding or [0.1] * 384,
    )


def _make_entity(name: str) -> Entity:
    return Entity(
        id=uuid4(),
        canonical_name=name,
        entity_type='Concept',
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )


def _make_unit_entity(unit_id, entity_id, vault_id) -> UnitEntity:
    return UnitEntity(
        unit_id=unit_id,
        entity_id=entity_id,
        vault_id=vault_id,
    )


# --------------------------------------------------------------------------- #
# DB-only integration tests (candidate retrieval)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_retrieval_entity_overlap(session: AsyncSession):
    """Units sharing entities appear as candidates via entity overlap."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit_a = _make_unit(note.id, vault_id, f'Unit A about topic {uuid4()}')
    unit_b = _make_unit(note.id, vault_id, f'Unit B about topic {uuid4()}')
    session.add_all([unit_a, unit_b])
    await session.flush()

    entity = _make_entity(f'shared-topic-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(unit_a.id, entity.id, vault_id),
            _make_unit_entity(unit_b.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    candidates = await get_candidates(session, unit_a, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert unit_b.id in candidate_ids
    assert unit_a.id not in candidate_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_retrieval_semantic(session: AsyncSession):
    """Units with similar embeddings appear as candidates via semantic similarity."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    shared_embedding = [0.5] * 384
    unit_a = _make_unit(note.id, vault_id, f'Semantic unit A {uuid4()}', embedding=shared_embedding)
    unit_b = _make_unit(note.id, vault_id, f'Semantic unit B {uuid4()}', embedding=shared_embedding)
    session.add_all([unit_a, unit_b])
    await session.commit()

    candidates = await get_candidates(session, unit_a, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert unit_b.id in candidate_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_candidates_no_links(
    session: AsyncSession,
):
    """Flagged unit with no entity overlap and dissimilar embedding produces no candidates."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit = _make_unit(
        note.id,
        vault_id,
        f'Isolated fact {uuid4()}',
        confidence=1.0,
        embedding=[0.99] + [0.0] * 383,
    )
    session.add(unit)
    await session.commit()

    candidates = await get_candidates(session, unit, vault_id, k=15, threshold=0.5)
    assert len(candidates) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_failure_does_not_crash(contradiction_config: ContradictionConfig):
    """detect_contradictions swallows exceptions from a broken session_factory."""
    engine = ContradictionEngine(lm=MagicMock(), config=contradiction_config)

    broken_factory = MagicMock()
    broken_factory.return_value.__aenter__ = AsyncMock(
        side_effect=RuntimeError('DB connection failed')
    )
    broken_factory.return_value.__aexit__ = AsyncMock()

    # Should not raise
    await engine.detect_contradictions(
        session_factory=broken_factory,
        document_id='test-doc',
        unit_ids=[uuid4()],
        vault_id=GLOBAL_VAULT_ID,
    )


# --------------------------------------------------------------------------- #
# LLM integration tests (real triage + classification calls)
# --------------------------------------------------------------------------- #


def _skip_without_api_key():
    """Skip test if no Google API key is available."""
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')


def _make_llm_engine(contradiction_config: ContradictionConfig) -> ContradictionEngine:
    """Create a ContradictionEngine with a real LLM."""
    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key)
    return ContradictionEngine(lm=lm, config=contradiction_config)


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_contradict_decreases_confidence(
    session: AsyncSession, contradiction_config: ContradictionConfig
):
    """Real LLM detects contradiction: 'backlog has 15 items' vs 'backlog has 5 items, not 15'."""
    _skip_without_api_key()
    vault_id = GLOBAL_VAULT_ID

    note_old = _make_note(vault_id, title='Sprint Review Jan')
    note_new = _make_note(vault_id, title='Sprint Review Feb')
    session.add_all([note_old, note_new])
    await session.flush()

    old_unit = _make_unit(
        note_old.id,
        vault_id,
        'The backlog has 15 items prioritized by impact.',
        confidence=1.0,
        event_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
    )
    new_unit = _make_unit(
        note_new.id,
        vault_id,
        'The backlog has 5 items, not 15. The count was corrected.',
        confidence=1.0,
        event_date=datetime(2025, 2, 15, tzinfo=timezone.utc),
    )
    session.add_all([old_unit, new_unit])
    await session.flush()

    entity = _make_entity(f'backlog-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = _make_llm_engine(contradiction_config)
    await engine._detect(session, [new_unit.id], vault_id)
    await session.commit()

    await session.refresh(old_unit)
    # The LLM should identify the new unit as corrective and contradict or weaken the old one
    assert old_unit.confidence < 1.0, (
        f'Expected old unit confidence to decrease, got {old_unit.confidence}'
    )

    links = (
        await session.exec(
            select(MemoryLink).where(
                MemoryLink.link_type.in_(['contradicts', 'weakens'])  # type: ignore
            )
        )
    ).all()
    assert len(links) >= 1, 'Expected at least one contradiction/weaken link'
    link = links[0]
    assert link.link_metadata is not None
    assert 'reasoning' in link.link_metadata


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_reinforce_agreeing_statements(
    session: AsyncSession, contradiction_config: ContradictionConfig
):
    """Real LLM detects reinforcement: two statements agreeing about the same fact."""
    _skip_without_api_key()
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id, title='Tech Stack Notes')
    session.add(note)
    await session.flush()

    unit_a = _make_unit(
        note.id,
        vault_id,
        'The team uses Python 3.12 as the standard runtime for all backend services.',
        confidence=0.8,
    )
    unit_b = _make_unit(
        note.id,
        vault_id,
        'As confirmed in the latest review, Python 3.12 remains the standard runtime.',
        confidence=0.8,
    )
    session.add_all([unit_a, unit_b])
    await session.flush()

    entity = _make_entity(f'python-runtime-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(unit_a.id, entity.id, vault_id),
            _make_unit_entity(unit_b.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = _make_llm_engine(contradiction_config)
    await engine._detect(session, [unit_b.id], vault_id)
    await session.commit()

    await session.refresh(unit_a)
    await session.refresh(unit_b)

    # LLM may flag unit_b as confirmatory → reinforce, or skip triage entirely (both valid).
    # If reinforced, confidence should go up. If triage skipped, confidence stays.
    assert unit_a.confidence >= 0.8, 'Confidence should not decrease for agreeing statements'
    assert unit_b.confidence >= 0.8, 'Confidence should not decrease for agreeing statements'


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_neutral_statements_no_links(
    session: AsyncSession, contradiction_config: ContradictionConfig
):
    """Real LLM: unrelated statements should produce no contradiction links."""
    _skip_without_api_key()
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id, title='Mixed Notes')
    session.add(note)
    await session.flush()

    unit_weather = _make_unit(note.id, vault_id, 'It rained heavily in Amsterdam on Tuesday.')
    unit_recipe = _make_unit(note.id, vault_id, 'The pasta recipe calls for 200g of flour.')
    session.add_all([unit_weather, unit_recipe])
    await session.commit()

    engine = _make_llm_engine(contradiction_config)
    await engine._detect(session, [unit_weather.id, unit_recipe.id], vault_id)
    await session.commit()

    # These are unrelated — triage should not flag them
    links = (
        await session.exec(
            select(MemoryLink).where(
                MemoryLink.link_type.in_(['contradicts', 'weakens'])  # type: ignore
            )
        )
    ).all()
    assert len(links) == 0, 'Unrelated statements should not produce contradiction links'


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_temporal_directionality(
    session: AsyncSession, contradiction_config: ContradictionConfig
):
    """Real LLM: newer correction supersedes older fact."""
    _skip_without_api_key()
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id, title='Leadership Updates')
    session.add(note)
    await session.flush()

    old_unit = _make_unit(
        note.id,
        vault_id,
        'The CEO of the company is Alice Johnson.',
        confidence=1.0,
        event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    new_unit = _make_unit(
        note.id,
        vault_id,
        'The CEO is now Bob Smith. Alice Johnson stepped down in December 2024.',
        confidence=1.0,
        event_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
    )
    session.add_all([old_unit, new_unit])
    await session.flush()

    entity = _make_entity(f'ceo-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = _make_llm_engine(contradiction_config)
    await engine._detect(session, [new_unit.id], vault_id)
    await session.commit()

    await session.refresh(old_unit)
    await session.refresh(new_unit)

    # The old unit should be superseded (lower confidence)
    assert old_unit.confidence < 1.0, (
        f'Old unit should be superseded, got confidence={old_unit.confidence}'
    )
    # The new unit should remain authoritative
    assert new_unit.confidence >= 1.0, (
        f'New unit should remain authoritative, got confidence={new_unit.confidence}'
    )

    links = (
        await session.exec(
            select(MemoryLink).where(
                MemoryLink.link_type.in_(['contradicts', 'weakens'])  # type: ignore
            )
        )
    ).all()
    assert len(links) >= 1
    # Verify directionality: authoritative (new) → superseded (old)
    link = links[0]
    assert link.from_unit_id == new_unit.id, 'Authoritative unit should be from_unit'
    assert link.to_unit_id == old_unit.id, 'Superseded unit should be to_unit'
