"""Test that deleting a note prunes stale evidence from mental model observations.

Reproduces the bug where mental model observations survive note deletion,
causing search to return virtual MemoryUnits with IDs that don't exist
in the database (lineage returns 404).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    Entity,
    MentalModel,
    MemoryUnit,
    Note,
    UnitEntity,
)
from memex_core.services.mental_model_cleanup import prune_stale_evidence
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prune_stale_evidence_removes_deleted_unit_references(session: AsyncSession):
    """prune_stale_evidence must remove evidence citing deleted memory units
    and drop observations that lose all evidence."""

    vault_id = GLOBAL_VAULT_ID
    entity_id = uuid.uuid4()
    note_a_id = uuid.uuid4()
    note_b_id = uuid.uuid4()
    unit_a_id = uuid.uuid4()
    unit_b_id = uuid.uuid4()
    obs_shared_id = uuid.uuid4()
    obs_only_a_id = uuid.uuid4()

    # Setup: entity, notes, units, links, mental model
    session.add(Entity(id=entity_id, canonical_name='Test Entity', vault_id=vault_id))
    session.add(Note(id=note_a_id, vault_id=vault_id, original_text='Note A'))
    session.add(Note(id=note_b_id, vault_id=vault_id, original_text='Note B'))
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add(
        MemoryUnit(
            id=unit_a_id,
            vault_id=vault_id,
            note_id=note_a_id,
            text='Fact A',
            fact_type=FactTypes.WORLD,
            embedding=[0.0] * 384,
            event_date=now,
        )
    )
    session.add(
        MemoryUnit(
            id=unit_b_id,
            vault_id=vault_id,
            note_id=note_b_id,
            text='Fact B',
            fact_type=FactTypes.WORLD,
            embedding=[0.0] * 384,
            event_date=now,
        )
    )
    await session.flush()

    session.add(UnitEntity(unit_id=unit_a_id, entity_id=entity_id))
    session.add(UnitEntity(unit_id=unit_b_id, entity_id=entity_id))

    mm = MentalModel(
        entity_id=entity_id,
        vault_id=vault_id,
        name='Test Entity',
        observations=[
            {
                'id': str(obs_shared_id),
                'title': 'Shared observation',
                'content': 'From both notes',
                'trend': 'new',
                'evidence': [
                    {'memory_id': str(unit_a_id), 'quote': 'A', 'relevance': 1.0},
                    {'memory_id': str(unit_b_id), 'quote': 'B', 'relevance': 1.0},
                ],
            },
            {
                'id': str(obs_only_a_id),
                'title': 'Only-A observation',
                'content': 'From note A only',
                'trend': 'new',
                'evidence': [
                    {'memory_id': str(unit_a_id), 'quote': 'A', 'relevance': 1.0},
                ],
            },
        ],
        version=1,
    )
    session.add(mm)
    await session.commit()

    # Act: prune evidence for unit_a (simulating note A deletion)
    await prune_stale_evidence(
        session,
        entity_ids={entity_id},
        deleted_unit_ids=[unit_a_id],
        vault_id=vault_id,
    )
    await session.commit()

    # Assert: reload mental model
    session.expire_all()
    mm_after = (
        await session.exec(select(MentalModel).where(col(MentalModel.entity_id) == entity_id))
    ).first()

    assert mm_after is not None, 'Mental model should survive (entity still linked to note B)'

    remaining_obs_ids = {obs['id'] for obs in mm_after.observations}

    # obs_only_a had ALL evidence from unit_a → should be removed
    assert str(obs_only_a_id) not in remaining_obs_ids, (
        'Observation with only deleted-unit evidence should be removed'
    )

    # obs_shared should survive with only unit_b evidence
    assert str(obs_shared_id) in remaining_obs_ids, 'Observation with mixed evidence should survive'
    shared_obs = next(o for o in mm_after.observations if o['id'] == str(obs_shared_id))
    assert len(shared_obs['evidence']) == 1
    assert shared_obs['evidence'][0]['memory_id'] == str(unit_b_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prune_deletes_model_when_all_observations_removed(session: AsyncSession):
    """When all observations lose their evidence, the mental model itself
    should be deleted."""

    vault_id = GLOBAL_VAULT_ID
    entity_id = uuid.uuid4()
    note_id = uuid.uuid4()
    unit_id = uuid.uuid4()

    session.add(Entity(id=entity_id, canonical_name='Lonely Entity', vault_id=vault_id))
    session.add(Note(id=note_id, vault_id=vault_id, original_text='Only note'))
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add(
        MemoryUnit(
            id=unit_id,
            vault_id=vault_id,
            note_id=note_id,
            text='Only fact',
            fact_type=FactTypes.WORLD,
            embedding=[0.0] * 384,
            event_date=now,
        )
    )
    await session.flush()
    session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id))

    mm = MentalModel(
        entity_id=entity_id,
        vault_id=vault_id,
        name='Lonely Entity',
        observations=[
            {
                'id': str(uuid.uuid4()),
                'title': 'Single observation',
                'content': 'Only evidence from the one note',
                'trend': 'new',
                'evidence': [
                    {'memory_id': str(unit_id), 'quote': 'Only', 'relevance': 1.0},
                ],
            },
        ],
        version=1,
    )
    session.add(mm)
    await session.commit()

    # Act: prune evidence for the only unit
    await prune_stale_evidence(
        session,
        entity_ids={entity_id},
        deleted_unit_ids=[unit_id],
        vault_id=vault_id,
    )
    await session.commit()

    # Assert: mental model should be deleted (zero observations remaining)
    session.expire_all()
    mm_after = (
        await session.exec(select(MentalModel).where(col(MentalModel.entity_id) == entity_id))
    ).first()
    assert mm_after is None, 'Mental model with zero observations should be deleted'
