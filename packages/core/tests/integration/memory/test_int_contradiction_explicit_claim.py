"""Integration tests for explicit-claim contradiction matching.

Verifies that ``MemoryUnit.claim_type`` flows end-to-end through the
contradiction engine: candidate retrieval narrows by ``target_entity_ids``,
the lower ``similarity_threshold_explicit_claim`` widens the candidate net,
and the resulting ``MemoryLink`` carries the explicit-claim metadata + weight.

Marked ``integration`` + ``llm_mock`` — real Postgres, mocked DSPy LM.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
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
        similarity_threshold_explicit_claim=0.35,
        claim_too_aggressive_max_links=5,
        max_candidates_per_unit=15,
        superseded_threshold=0.3,
    )


def _make_note(vault_id) -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title='Test Note',
        content_hash=str(uuid4()),
        original_text=f'Test content {uuid4()}',
    )


def _make_unit(
    note_id,
    vault_id,
    text: str,
    *,
    claim_type: str | None = None,
    unit_metadata: dict | None = None,
    embedding: list[float] | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=1.0,
        event_date=datetime.now(timezone.utc),
        embedding=embedding or [0.1] * 384,
        claim_type=claim_type,
        unit_metadata=unit_metadata or {},
    )


def _make_entity(name: str) -> Entity:
    return Entity(
        id=uuid4(),
        canonical_name=name,
        entity_type='Concept',
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )


@pytest.mark.integration
@pytest.mark.llm_mock
@pytest.mark.asyncio
async def test_get_candidates_filters_by_target_entity_ids(session: AsyncSession) -> None:
    """The entity-overlap path narrows to only listed target entity IDs."""
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    # Three units: A (the claim), B (shares target entity), C (shares a
    # different entity). Expect only B to come back when target=[target_entity].
    unit_a = _make_unit(note.id, vault_id, f'claim unit {uuid4()}')
    unit_b = _make_unit(note.id, vault_id, f'prior on-topic {uuid4()}')
    unit_c = _make_unit(note.id, vault_id, f'prior off-topic {uuid4()}')
    session.add_all([unit_a, unit_b, unit_c])
    await session.flush()

    target_entity = _make_entity(f'target-{uuid4()}')
    other_entity = _make_entity(f'other-{uuid4()}')
    session.add_all([target_entity, other_entity])
    await session.flush()

    session.add_all(
        [
            UnitEntity(unit_id=unit_a.id, entity_id=target_entity.id, vault_id=vault_id),
            UnitEntity(unit_id=unit_a.id, entity_id=other_entity.id, vault_id=vault_id),
            UnitEntity(unit_id=unit_b.id, entity_id=target_entity.id, vault_id=vault_id),
            UnitEntity(unit_id=unit_c.id, entity_id=other_entity.id, vault_id=vault_id),
        ]
    )
    await session.commit()

    # No filter: B and C should both surface (entity overlap with A).
    unrestricted = await get_candidates(session, unit_a, vault_id, k=15, threshold=0.5)
    unrestricted_ids = {c.id for c in unrestricted}
    assert unit_b.id in unrestricted_ids
    assert unit_c.id in unrestricted_ids

    # With target filter: only B should surface.
    restricted = await get_candidates(
        session,
        unit_a,
        vault_id,
        k=15,
        threshold=0.5,
        target_entity_ids=[target_entity.id],
    )
    restricted_ids = {c.id for c in restricted}
    assert unit_b.id in restricted_ids
    assert unit_c.id not in restricted_ids


@pytest.mark.integration
@pytest.mark.llm_mock
@pytest.mark.asyncio
async def test_explicit_claim_link_carries_claim_type_metadata(
    session: AsyncSession, contradiction_config: ContradictionConfig
) -> None:
    """End-to-end: a unit with ``claim_type='contradiction'`` flagged by triage
    yields a MemoryLink whose ``link_metadata`` carries ``claim_type`` and whose
    ``weight`` matches the policy in ``_weight_for_relation``.
    """
    vault_id = GLOBAL_VAULT_ID

    note = _make_note(vault_id)
    session.add(note)
    await session.flush()

    target_entity = _make_entity(f'tgt-{uuid4()}')
    session.add(target_entity)
    await session.flush()

    shared_emb = [0.5] * 384
    prior = _make_unit(note.id, vault_id, f'we use Postgres {uuid4()}', embedding=shared_emb)
    claim = _make_unit(
        note.id,
        vault_id,
        f'we no longer use Postgres {uuid4()}',
        claim_type='contradiction',
        unit_metadata={
            'claim_target': {
                'target_topic': 'Postgres decision',
                'target_entity_ids': [str(target_entity.id)],
            }
        },
        embedding=shared_emb,
    )
    session.add_all([prior, claim])
    await session.flush()

    session.add_all(
        [
            UnitEntity(unit_id=prior.id, entity_id=target_entity.id, vault_id=vault_id),
            UnitEntity(unit_id=claim.id, entity_id=target_entity.id, vault_id=vault_id),
        ]
    )
    await session.commit()

    mock_lm = MagicMock()
    engine = ContradictionEngine(lm=mock_lm, config=contradiction_config)

    triage_result = MagicMock()
    triage_result.flagged_ids = [str(claim.id)]

    classify_result = MagicMock()
    rel = MagicMock()
    rel.relation = 'contradict'
    rel.authoritative = 'new'
    rel.reasoning = 'mock'
    rel.existing_id = str(prior.id)
    classify_result.relationships = [rel]

    async def fake_run(**kwargs):
        if kwargs['operation_name'] == 'contradiction.triage':
            return triage_result
        return classify_result

    with patch('memex_core.memory.contradiction.engine.run_dspy_operation', new=fake_run):
        await engine._detect(session, [claim.id], vault_id)
    await session.commit()

    links_stmt = select(MemoryLink).where(
        MemoryLink.from_unit_id == claim.id, MemoryLink.to_unit_id == prior.id
    )
    result = await session.exec(links_stmt)
    links = list(result.all())
    assert len(links) == 1
    link = links[0]
    assert link.link_type == 'contradicts'
    assert link.weight == 1.0
    assert link.link_metadata.get('claim_type') == 'contradiction'
