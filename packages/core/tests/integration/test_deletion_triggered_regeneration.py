"""Integration tests for deletion-triggered regeneration wiring (AC-012/013/014).

Verifies that delete_note(), set_note_status('archived'), and delete_memory_unit()
each trigger all three mechanisms:
  1. Evidence pruned from surviving mental models
  2. ReflectionQueue entries created with priority 1.0 for affected entities
  3. VaultSummary.needs_regeneration = True
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
    ReflectionQueue,
    UnitEntity,
    VaultSummary,
)
from memex_core.services.notes import await_background_tasks as await_notes_bg
from memex_core.services.stats import await_background_tasks as await_stats_bg
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes


async def _seed_two_notes_with_shared_entity(session: AsyncSession, vault_id: uuid.UUID):
    """Seed two notes sharing one entity, with a mental model citing both units.

    Note A (the target) and Note B (the survivor) both contribute evidence.
    After deleting/archiving Note A, the entity survives (still linked to Note B),
    so the ReflectionQueue item won't be cascade-deleted by entity cleanup.

    Returns a dict with all created IDs for assertion.
    """
    note_a_id = uuid.uuid4()
    note_b_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    unit_a_id = uuid.uuid4()
    unit_b_id = uuid.uuid4()
    obs_shared_id = uuid.uuid4()
    obs_only_a_id = uuid.uuid4()

    session.add(
        Entity(
            id=entity_id,
            canonical_name=f'Entity-{entity_id.hex[:8]}',
            vault_id=vault_id,
            mention_count=2,
        )
    )
    session.add(
        Note(
            id=note_a_id,
            vault_id=vault_id,
            original_text=f'Note A {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
    )
    session.add(
        Note(
            id=note_b_id,
            vault_id=vault_id,
            original_text=f'Note B {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
    )
    await session.flush()

    now = datetime.now(timezone.utc)
    session.add(
        MemoryUnit(
            id=unit_a_id,
            vault_id=vault_id,
            note_id=note_a_id,
            text=f'Fact A {uuid.uuid4()}',
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
            text=f'Fact B {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.0] * 384,
            event_date=now,
        )
    )
    await session.flush()

    session.add(UnitEntity(unit_id=unit_a_id, entity_id=entity_id, vault_id=vault_id))
    session.add(UnitEntity(unit_id=unit_b_id, entity_id=entity_id, vault_id=vault_id))

    mm = MentalModel(
        entity_id=entity_id,
        vault_id=vault_id,
        name=f'Entity-{entity_id.hex[:8]}',
        observations=[
            {
                'id': str(obs_shared_id),
                'title': 'Shared observation',
                'content': 'Evidence from both notes',
                'trend': 'new',
                'evidence': [
                    {'memory_id': str(unit_a_id), 'quote': 'from A', 'relevance': 1.0},
                    {'memory_id': str(unit_b_id), 'quote': 'from B', 'relevance': 1.0},
                ],
            },
            {
                'id': str(obs_only_a_id),
                'title': 'Only-A observation',
                'content': 'Evidence only from note A',
                'trend': 'new',
                'evidence': [
                    {'memory_id': str(unit_a_id), 'quote': 'only A', 'relevance': 1.0},
                ],
            },
        ],
        version=1,
    )
    session.add(mm)

    # Create a VaultSummary so mark_needs_regeneration has something to update
    session.add(
        VaultSummary(
            vault_id=vault_id,
            narrative='Test summary',
            version=1,
            notes_incorporated=2,
            needs_regeneration=False,
        )
    )

    await session.commit()

    return {
        'note_a_id': note_a_id,
        'note_b_id': note_b_id,
        'entity_id': entity_id,
        'unit_a_id': unit_a_id,
        'unit_b_id': unit_b_id,
        'obs_shared_id': obs_shared_id,
        'obs_only_a_id': obs_only_a_id,
    }


async def _assert_all_three_mechanisms(
    metastore,
    entity_id: uuid.UUID,
    vault_id: uuid.UUID,
    unit_b_id: uuid.UUID,
    obs_shared_id: uuid.UUID,
    obs_only_a_id: uuid.UUID,
):
    """Assert evidence pruned, queue entry created, and vault summary flagged.

    Uses the two-note setup: unit_a was deleted, unit_b survives.
    obs_only_a (citing only unit_a) should be removed.
    obs_shared (citing both) should survive with only unit_b evidence.
    """
    async with metastore.session() as verify:
        # 1. Evidence pruned from surviving mental model
        mm = (
            await verify.exec(select(MentalModel).where(col(MentalModel.entity_id) == entity_id))
        ).first()
        assert mm is not None, 'Mental model should survive (entity still has unit_b)'

        remaining_obs_ids = {obs['id'] for obs in mm.observations}
        assert str(obs_only_a_id) not in remaining_obs_ids, (
            'Observation with only deleted-unit evidence should be removed'
        )
        assert str(obs_shared_id) in remaining_obs_ids, (
            'Observation with mixed evidence should survive'
        )
        shared_obs = next(o for o in mm.observations if o['id'] == str(obs_shared_id))
        assert len(shared_obs['evidence']) == 1
        assert shared_obs['evidence'][0]['memory_id'] == str(unit_b_id)

        # 2. ReflectionQueue entry with priority 1.0
        queue_item = (
            await verify.exec(
                select(ReflectionQueue)
                .where(col(ReflectionQueue.entity_id) == entity_id)
                .where(col(ReflectionQueue.vault_id) == vault_id)
            )
        ).first()
        assert queue_item is not None, 'ReflectionQueue entry should exist for affected entity'
        assert queue_item.priority_score == 1.0, 'Priority should be 1.0 for deletion events'
        assert str(queue_item.status) == 'pending', 'Status should be PENDING'

        # 3. VaultSummary.needs_regeneration = True
        summary = (
            await verify.exec(select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id))
        ).first()
        assert summary is not None, 'VaultSummary should exist'
        assert summary.needs_regeneration is True, (
            'VaultSummary.needs_regeneration should be True after deletion'
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_triggers_all_mechanisms(api, metastore, session: AsyncSession):
    """AC-012: delete_note() prunes evidence, queues entities, and flags vault summary."""
    vault_id = GLOBAL_VAULT_ID
    ids = await _seed_two_notes_with_shared_entity(session, vault_id)

    await api.delete_note(ids['note_a_id'])
    await await_notes_bg()

    await _assert_all_three_mechanisms(
        metastore,
        ids['entity_id'],
        vault_id,
        ids['unit_b_id'],
        ids['obs_shared_id'],
        ids['obs_only_a_id'],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_note_status_archived_triggers_all_mechanisms(
    api, metastore, session: AsyncSession
):
    """AC-013: set_note_status('archived') prunes evidence, queues entities, flags summary."""
    vault_id = GLOBAL_VAULT_ID
    ids = await _seed_two_notes_with_shared_entity(session, vault_id)

    await api.set_note_status(ids['note_a_id'], 'archived')
    await await_notes_bg()

    await _assert_all_three_mechanisms(
        metastore,
        ids['entity_id'],
        vault_id,
        ids['unit_b_id'],
        ids['obs_shared_id'],
        ids['obs_only_a_id'],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_memory_unit_triggers_all_mechanisms(api, metastore, session: AsyncSession):
    """AC-014: delete_memory_unit() prunes evidence, queues entities, flags summary."""
    vault_id = GLOBAL_VAULT_ID
    ids = await _seed_two_notes_with_shared_entity(session, vault_id)

    await api.delete_memory_unit(ids['unit_a_id'])
    await await_stats_bg()

    await _assert_all_three_mechanisms(
        metastore,
        ids['entity_id'],
        vault_id,
        ids['unit_b_id'],
        ids['obs_shared_id'],
        ids['obs_only_a_id'],
    )
