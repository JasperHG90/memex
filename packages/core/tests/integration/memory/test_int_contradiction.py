"""Integration tests for contradiction detection."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock
from uuid import uuid4

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


@pytest.fixture
def mock_lm() -> MagicMock:
    return MagicMock()


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
        canonical_name=name,
        entity_type='Concept',
    )


def _make_unit_entity(unit_id, entity_id, vault_id) -> UnitEntity:
    return UnitEntity(
        unit_id=unit_id,
        entity_id=entity_id,
        vault_id=vault_id,
    )


def _mock_triage_result(flagged_ids: list[str]):
    """Create a mock result for the triage LLM call."""
    result = MagicMock()
    result.flagged_ids = flagged_ids
    return result


def _mock_classify_result(relationships: list[dict]):
    """Create a mock result for the classify LLM call."""
    result = MagicMock()
    result.relationships = relationships
    return result


# --------------------------------------------------------------------------- #
# Happy path tests
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contradict_decreases_confidence(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Contradicting unit decreases the old unit's confidence by 2*alpha."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    old_unit = _make_unit(note.id, vault_id, f'The backlog has 15 items {uuid4()}', confidence=1.0)
    session.add(old_unit)
    await session.flush()

    entity = _make_entity(f'backlog-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add(_make_unit_entity(old_unit.id, entity.id, vault_id))

    new_unit = _make_unit(
        note.id, vault_id, f'The backlog has 5 items, not 15 {uuid4()}', confidence=1.0
    )
    session.add(new_unit)
    await session.flush()

    session.add(_make_unit_entity(new_unit.id, entity.id, vault_id))
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(new_unit.id)]), MagicMock()
            return _mock_classify_result(
                [
                    {
                        'existing_id': str(old_unit.id),
                        'relation': 'contradict',
                        'authoritative': 'new',
                        'reasoning': 'The new unit corrects the count.',
                    }
                ]
            ), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [new_unit.id], vault_id)
        await session.commit()

    await session.refresh(old_unit)
    expected_conf = 1.0 - 2 * contradiction_config.alpha
    assert old_unit.confidence == pytest.approx(expected_conf)

    links = (
        await session.exec(select(MemoryLink).where(MemoryLink.link_type == 'contradicts'))
    ).all()
    assert len(links) == 1
    assert links[0].from_unit_id == new_unit.id
    assert links[0].to_unit_id == old_unit.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reinforce_increases_confidence(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Reinforcing classification increases both units' confidence by alpha."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit_a = _make_unit(note.id, vault_id, f'The team uses Python 3.12 {uuid4()}', confidence=0.8)
    unit_b = _make_unit(
        note.id, vault_id, f'Python 3.12 is the team standard {uuid4()}', confidence=0.8
    )
    session.add_all([unit_a, unit_b])
    await session.flush()

    entity = _make_entity(f'python-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(unit_a.id, entity.id, vault_id),
            _make_unit_entity(unit_b.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(unit_b.id)]), MagicMock()
            return _mock_classify_result(
                [
                    {
                        'existing_id': str(unit_a.id),
                        'relation': 'reinforce',
                        'authoritative': 'new',
                        'reasoning': 'Both confirm Python 3.12.',
                    }
                ]
            ), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [unit_b.id], vault_id)
        await session.commit()

    await session.refresh(unit_a)
    await session.refresh(unit_b)
    expected = 0.8 + contradiction_config.alpha
    assert unit_a.confidence == pytest.approx(expected)
    assert unit_b.confidence == pytest.approx(expected)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weaken_partial_decrease(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Weaken classification decreases superseded unit's confidence by alpha (not 2*alpha)."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    old_unit = _make_unit(
        note.id, vault_id, f'The deadline is likely in March {uuid4()}', confidence=1.0
    )
    new_unit = _make_unit(
        note.id, vault_id, f'The deadline might be pushed to April {uuid4()}', confidence=1.0
    )
    session.add_all([old_unit, new_unit])
    await session.flush()

    entity = _make_entity(f'deadline-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(new_unit.id)]), MagicMock()
            return _mock_classify_result(
                [
                    {
                        'existing_id': str(old_unit.id),
                        'relation': 'weaken',
                        'authoritative': 'new',
                        'reasoning': 'New info casts doubt on original deadline.',
                    }
                ]
            ), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [new_unit.id], vault_id)
        await session.commit()

    await session.refresh(old_unit)
    expected = 1.0 - contradiction_config.alpha
    assert old_unit.confidence == pytest.approx(expected)

    links = (await session.exec(select(MemoryLink).where(MemoryLink.link_type == 'weakens'))).all()
    assert len(links) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_link_metadata_populated(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Verify link_metadata contains all expected provenance fields."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id, title='Corrective Report')
    session.add(note)
    await session.flush()

    old_unit = _make_unit(note.id, vault_id, f'Revenue was $10M {uuid4()}', confidence=1.0)
    new_unit = _make_unit(note.id, vault_id, f'Revenue was actually $8M {uuid4()}', confidence=1.0)
    session.add_all([old_unit, new_unit])
    await session.flush()

    entity = _make_entity(f'revenue-{uuid4()}')
    session.add(entity)
    await session.flush()

    session.add_all(
        [
            _make_unit_entity(old_unit.id, entity.id, vault_id),
            _make_unit_entity(new_unit.id, entity.id, vault_id),
        ]
    )
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(new_unit.id)]), MagicMock()
            return _mock_classify_result(
                [
                    {
                        'existing_id': str(old_unit.id),
                        'relation': 'contradict',
                        'authoritative': 'new',
                        'reasoning': 'Corrected revenue figure.',
                    }
                ]
            ), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [new_unit.id], vault_id)
        await session.commit()

    links = (
        await session.exec(select(MemoryLink).where(MemoryLink.link_type == 'contradicts'))
    ).all()
    assert len(links) == 1
    meta = links[0].link_metadata
    assert meta['authoritative_unit_id'] == str(new_unit.id)
    assert meta['superseded_unit_id'] == str(old_unit.id)
    assert meta['reasoning'] == 'Corrected revenue figure.'
    assert 'temporal_basis' in meta
    assert meta['superseding_note_title'] == 'Corrective Report'


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

    # Same embedding = cosine distance 0 = very similar
    shared_embedding = [0.5] * 384
    unit_a = _make_unit(note.id, vault_id, f'Semantic unit A {uuid4()}', embedding=shared_embedding)
    unit_b = _make_unit(note.id, vault_id, f'Semantic unit B {uuid4()}', embedding=shared_embedding)
    session.add_all([unit_a, unit_b])
    await session.commit()

    candidates = await get_candidates(session, unit_a, vault_id, k=15, threshold=0.5)
    candidate_ids = [c.id for c in candidates]
    assert unit_b.id in candidate_ids


# --------------------------------------------------------------------------- #
# Unhappy path tests
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_corrective_language_no_links(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """When triage returns empty flagged_ids, no links or confidence changes occur."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    unit = _make_unit(note.id, vault_id, f'Neutral statement {uuid4()}', confidence=1.0)
    session.add(unit)
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:
        mock_dspy.return_value = (_mock_triage_result([]), MagicMock())

        await engine._detect(session, [unit.id], vault_id)
        await session.commit()

    await session.refresh(unit)
    assert unit.confidence == 1.0

    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_failure_does_not_crash(
    contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """detect_contradictions swallows exceptions from a broken session_factory."""
    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_candidates_no_links(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Flagged unit with no entity overlap and dissimilar embedding produces no links."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    # Use a very different embedding from everything else
    unit = _make_unit(
        note.id,
        vault_id,
        f'Isolated fact {uuid4()}',
        confidence=1.0,
        embedding=[0.99] + [0.0] * 383,
    )
    session.add(unit)
    await session.commit()

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(unit.id)]), MagicMock()
            # Should not reach classify since candidates are empty
            return _mock_classify_result([]), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [unit.id], vault_id)
        await session.commit()

    await session.refresh(unit)
    assert unit.confidence == 1.0

    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporal_directionality_newer_wins(
    session: AsyncSession, contradiction_config: ContradictionConfig, mock_lm: MagicMock
):
    """Newer unit is authoritative; older unit gets superseded."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    old_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    new_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

    old_unit = _make_unit(
        note.id,
        vault_id,
        f'CEO is Alice {uuid4()}',
        confidence=1.0,
        event_date=old_date,
    )
    new_unit = _make_unit(
        note.id,
        vault_id,
        f'CEO is now Bob {uuid4()}',
        confidence=1.0,
        event_date=new_date,
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

    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_dspy:

        async def dspy_side_effect(*, lm, predictor, input_kwargs):
            if 'units' in input_kwargs:
                return _mock_triage_result([str(new_unit.id)]), MagicMock()
            return _mock_classify_result(
                [
                    {
                        'existing_id': str(old_unit.id),
                        'relation': 'contradict',
                        'authoritative': 'new',
                        'reasoning': 'Leadership change.',
                    }
                ]
            ), MagicMock()

        mock_dspy.side_effect = dspy_side_effect

        await engine._detect(session, [new_unit.id], vault_id)
        await session.commit()

    await session.refresh(old_unit)
    await session.refresh(new_unit)

    # Old unit is superseded, confidence decreased
    assert old_unit.confidence == pytest.approx(1.0 - 2 * contradiction_config.alpha)
    # New unit (authoritative) confidence unchanged
    assert new_unit.confidence == 1.0

    links = (
        await session.exec(select(MemoryLink).where(MemoryLink.link_type == 'contradicts'))
    ).all()
    assert len(links) == 1
    assert links[0].from_unit_id == new_unit.id  # authoritative
    assert links[0].to_unit_id == old_unit.id  # superseded
