"""Integration tests for contradiction detection.

Tests marked @pytest.mark.integration use real DB (testcontainers) but mock LLM calls.
Tests marked @pytest.mark.llm use real DB AND real LLM calls (require GOOGLE_API_KEY).
"""

import math
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


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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
    note_noise = _make_note(vault_id, title='Unrelated Meeting')
    session.add_all([note_old, note_new, note_noise])
    await session.flush()

    # Use similar embeddings for related units, orthogonal for noise
    related_embedding = [0.5] * 384
    noise_embedding = [0.0] * 192 + [1.0] * 192

    old_unit = _make_unit(
        note_old.id,
        vault_id,
        'The backlog has 15 items prioritized by impact.',
        confidence=1.0,
        event_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
        embedding=related_embedding,
    )
    new_unit = _make_unit(
        note_new.id,
        vault_id,
        'The backlog has 5 items, not 15. The count was corrected.',
        confidence=1.0,
        event_date=datetime(2025, 2, 15, tzinfo=timezone.utc),
        embedding=related_embedding,
    )
    noise_unit = _make_unit(
        note_noise.id,
        vault_id,
        f'The weather forecast predicts rain tomorrow {uuid4()}.',
        confidence=1.0,
        embedding=noise_embedding,
    )
    session.add_all([old_unit, new_unit, noise_unit])
    await session.flush()

    entity = _make_entity(f'backlog-{uuid4()}')
    noise_entity = _make_entity(f'weather-{uuid4()}')
    session.add_all([entity, noise_entity])
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
            _make_unit_entity(noise_unit.id, noise_entity.id, vault_id),
        ]
    )
    await session.commit()

    # Verify candidate retrieval finds the related unit with meaningful similarity
    candidates = await get_candidates(session, new_unit, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert old_unit.id in candidate_ids, 'Related unit should appear as candidate'
    assert noise_unit.id not in candidate_ids, 'Noise unit should not appear as candidate'
    sim = _cosine_similarity(new_unit.embedding, old_unit.embedding)
    assert sim >= 0.5, f'Related units should have cosine similarity >= 0.5, got {sim:.3f}'

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
    note_noise = _make_note(vault_id, title='Lunch Menu')
    session.add_all([note, note_noise])
    await session.flush()

    runtime_embedding = [0.6] * 384
    noise_embedding = [0.0] * 192 + [1.0] * 192

    unit_a = _make_unit(
        note.id,
        vault_id,
        'The team uses Python 3.12 as the standard runtime for all backend services.',
        confidence=0.8,
        embedding=runtime_embedding,
    )
    unit_b = _make_unit(
        note.id,
        vault_id,
        'As confirmed in the latest review, Python 3.12 remains the standard runtime.',
        confidence=0.8,
        embedding=runtime_embedding,
    )
    noise_unit = _make_unit(
        note_noise.id,
        vault_id,
        f'Today lunch menu features grilled salmon {uuid4()}.',
        confidence=1.0,
        embedding=noise_embedding,
    )
    session.add_all([unit_a, unit_b, noise_unit])
    await session.flush()

    entity = _make_entity(f'python-runtime-{uuid4()}')
    noise_entity = _make_entity(f'lunch-{uuid4()}')
    session.add_all([entity, noise_entity])
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(unit_a.id, entity.id, vault_id),
            _make_unit_entity(unit_b.id, entity.id, vault_id),
            _make_unit_entity(noise_unit.id, noise_entity.id, vault_id),
        ]
    )
    await session.commit()

    # Verify candidate retrieval finds the related unit, not noise
    candidates = await get_candidates(session, unit_b, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert unit_a.id in candidate_ids, 'Agreeing unit should appear as candidate'
    assert noise_unit.id not in candidate_ids, 'Noise unit should not appear as candidate'
    sim = _cosine_similarity(unit_a.embedding, unit_b.embedding)
    assert sim >= 0.5, f'Agreeing units should have cosine similarity >= 0.5, got {sim:.3f}'

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
    note_noise = _make_note(vault_id, title='Kitchen Inventory')
    session.add_all([note, note_noise])
    await session.flush()

    # Use similar embeddings for CEO units, orthogonal for noise
    ceo_embedding = [0.7] * 384
    noise_embedding = [0.0] * 192 + [1.0] * 192

    old_unit = _make_unit(
        note.id,
        vault_id,
        'The CEO of the company is Alice Johnson.',
        confidence=1.0,
        event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        embedding=ceo_embedding,
    )
    new_unit = _make_unit(
        note.id,
        vault_id,
        'The CEO is now Bob Smith. Alice Johnson stepped down in December 2024.',
        confidence=1.0,
        event_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
        embedding=ceo_embedding,
    )
    noise_unit = _make_unit(
        note_noise.id,
        vault_id,
        f'We need to restock paper towels in the kitchen {uuid4()}.',
        confidence=1.0,
        embedding=noise_embedding,
    )
    session.add_all([old_unit, new_unit, noise_unit])
    await session.flush()

    entity = _make_entity(f'ceo-{uuid4()}')
    noise_entity = _make_entity(f'kitchen-{uuid4()}')
    session.add_all([entity, noise_entity])
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
            _make_unit_entity(noise_unit.id, noise_entity.id, vault_id),
        ]
    )
    await session.commit()

    # Verify candidate retrieval finds related unit, not noise
    candidates = await get_candidates(session, new_unit, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert old_unit.id in candidate_ids, 'Old CEO unit should appear as candidate'
    assert noise_unit.id not in candidate_ids, 'Noise unit should not appear as candidate'
    sim = _cosine_similarity(new_unit.embedding, old_unit.embedding)
    assert sim >= 0.5, f'CEO units should have cosine similarity >= 0.5, got {sim:.3f}'

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


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_no_cross_contamination_between_similar_topics(
    session: AsyncSession, contradiction_config: ContradictionConfig
):
    """Real LLM: CEO changes at different companies must not cross-link.

    Two semantically similar statements share the 'CEO' entity type but refer
    to different companies.  The LLM must recognise they are independent facts
    and NOT create contradiction/weaken links between them.
    """
    _skip_without_api_key()
    vault_id = GLOBAL_VAULT_ID

    note_acme = _make_note(vault_id, title='Acme Corp Board Minutes')
    note_globex = _make_note(vault_id, title='Globex Inc Leadership')
    session.add_all([note_acme, note_globex])
    await session.flush()

    # Similar embeddings — both talk about CEO changes, so they look alike
    ceo_embedding = [0.7] * 384

    acme_old = _make_unit(
        note_acme.id,
        vault_id,
        'The CEO of Acme Corp is Alice Johnson.',
        confidence=1.0,
        event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        embedding=ceo_embedding,
    )
    acme_new = _make_unit(
        note_acme.id,
        vault_id,
        'Bob Smith replaced Alice Johnson as CEO of Acme Corp in January 2025.',
        confidence=1.0,
        event_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
        embedding=ceo_embedding,
    )
    globex_unit = _make_unit(
        note_globex.id,
        vault_id,
        'The CEO of Globex Inc is Carol Williams. She was appointed in March 2024.',
        confidence=1.0,
        event_date=datetime(2024, 3, 1, tzinfo=timezone.utc),
        embedding=ceo_embedding,
    )
    session.add_all([acme_old, acme_new, globex_unit])
    await session.flush()

    # Shared 'CEO' entity — all three units are linked to it, making them candidates
    ceo_entity = _make_entity(f'ceo-{uuid4()}')
    acme_entity = _make_entity(f'acme-corp-{uuid4()}')
    globex_entity = _make_entity(f'globex-inc-{uuid4()}')
    session.add_all([ceo_entity, acme_entity, globex_entity])
    await session.flush()

    session.add_all(
        [
            # All share the CEO entity
            _make_unit_entity(acme_old.id, ceo_entity.id, vault_id),
            _make_unit_entity(acme_new.id, ceo_entity.id, vault_id),
            _make_unit_entity(globex_unit.id, ceo_entity.id, vault_id),
            # Company-specific entities
            _make_unit_entity(acme_old.id, acme_entity.id, vault_id),
            _make_unit_entity(acme_new.id, acme_entity.id, vault_id),
            _make_unit_entity(globex_unit.id, globex_entity.id, vault_id),
        ]
    )
    await session.commit()

    # All three should appear as candidates for each other (same entity + same embedding)
    candidates = await get_candidates(session, acme_new, vault_id, k=15, threshold=0.5)
    candidate_ids = {c.id for c in candidates}
    assert acme_old.id in candidate_ids, 'Same-company old unit should be a candidate'
    assert globex_unit.id in candidate_ids, (
        'Cross-company unit should be a candidate (shared CEO entity + similar embedding)'
    )

    engine = _make_llm_engine(contradiction_config)
    await engine._detect(session, [acme_new.id], vault_id)
    await session.commit()

    await session.refresh(acme_old)
    await session.refresh(globex_unit)

    # Acme old unit should be superseded (same company, real contradiction)
    assert acme_old.confidence < 1.0, (
        f'Acme old CEO unit should be superseded, got confidence={acme_old.confidence}'
    )

    # Globex unit should be UNTOUCHED — different company, no contradiction
    assert globex_unit.confidence == 1.0, (
        f'Globex CEO unit should be untouched, got confidence={globex_unit.confidence}'
    )

    # Verify no cross-company links exist
    links = (
        await session.exec(
            select(MemoryLink).where(
                MemoryLink.link_type.in_(['contradicts', 'weakens'])  # type: ignore
            )
        )
    ).all()
    link_pairs = {(link.from_unit_id, link.to_unit_id) for link in links}
    assert (acme_new.id, globex_unit.id) not in link_pairs, (
        'Should not create cross-company contradiction link'
    )
    assert (globex_unit.id, acme_new.id) not in link_pairs, (
        'Should not create cross-company contradiction link (reverse)'
    )
