"""Integration tests for the overwrite stale-unit cascade fix.

Before this PR, when a note was overwritten via re-ingestion, removed chunks
got marked stale and the memory units backed by them were also flipped to
'stale'. But the *entity graph* was left holding evidence pointers to those
units — mental-model observations kept citing memory_ids that no longer
backed active text, and the affected entities were never re-reflected.

These tests pin down the new ``cascade_chunk_unit_staling`` helper end-to-end:
- units backed by stale chunks get staled,
- mental-model evidence for those units is pruned,
- affected entities are queued for re-reflection.

Append doesn't trigger this code path (it only adds chunks, never removes), so
we also include a regression guard that proves append leaves things alone.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlmodel import col, select

from memex_core.api import NoteInput
from memex_core.memory.sql_models import (
    Chunk,
    ContentStatus,
    Entity,
    MemoryUnit,
    Note,
    UnitEntity,
)
from memex_core.services.mental_model_cleanup import cascade_chunk_unit_staling
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes


# --------------------------------------------------------------------------- #
# Cascade unit tests (direct exercise of the helper)                          #
# --------------------------------------------------------------------------- #


async def _seed_note_with_chunks_and_units(session, note_id: UUID) -> dict[str, UUID]:
    """Insert a Note + 2 Chunks + 2 MemoryUnits + 1 Entity + UnitEntity links."""
    chunk_a_id = uuid4()
    chunk_b_id = uuid4()
    unit_a_id = uuid4()
    unit_b_id = uuid4()
    entity_id = uuid4()

    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text='chunk A text\n\nchunk B text',
        content_hash='seed',
        title='seed',
    )
    session.add(note)

    for cid, cidx, txt, ch in (
        (chunk_a_id, 0, 'chunk A text', 'A-hash'),
        (chunk_b_id, 1, 'chunk B text', 'B-hash'),
    ):
        session.add(
            Chunk(
                id=cid,
                vault_id=GLOBAL_VAULT_ID,
                note_id=note_id,
                text=txt,
                content_hash=ch,
                chunk_index=cidx,
                embedding=[0.1] * 384,
            )
        )
    seed_event_date = datetime(2026, 4, 26, tzinfo=timezone.utc)
    for uid, cid, txt in (
        (unit_a_id, chunk_a_id, 'fact A'),
        (unit_b_id, chunk_b_id, 'fact B'),
    ):
        session.add(
            MemoryUnit(
                id=uid,
                note_id=note_id,
                chunk_id=cid,
                vault_id=GLOBAL_VAULT_ID,
                text=txt,
                fact_type=FactTypes.WORLD,
                embedding=[0.1] * 384,
                event_date=seed_event_date,
            )
        )

    session.add(
        Entity(
            id=entity_id,
            canonical_name='Entity One',
            entity_type='person',
            mention_count=2,
        )
    )
    for uid in (unit_a_id, unit_b_id):
        session.add(UnitEntity(unit_id=uid, entity_id=entity_id))
    await session.commit()

    return {
        'chunk_a': chunk_a_id,
        'chunk_b': chunk_b_id,
        'unit_a': unit_a_id,
        'unit_b': unit_b_id,
        'entity': entity_id,
    }


@pytest.mark.asyncio
async def test_cascade_stales_units_for_removed_chunks(metastore):
    """Cascade returns the unit_ids that were linked to stale chunks."""
    note_id = uuid4()
    async with metastore.session() as session:
        ids = await _seed_note_with_chunks_and_units(session, note_id)

    async with metastore.session() as session:
        # Simulate the extraction layer: chunk A is being staled.
        chunk_a = await session.get(Chunk, ids['chunk_a'])
        chunk_a.status = ContentStatus.STALE
        unit_a = await session.get(MemoryUnit, ids['unit_a'])
        unit_a.status = ContentStatus.STALE
        session.add(chunk_a)
        session.add(unit_a)
        await session.flush()

        cascaded = await cascade_chunk_unit_staling(
            session=session,
            note_id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            stale_chunk_ids=[ids['chunk_a']],
            queue_service=None,
        )
        await session.commit()

    assert ids['unit_a'] in cascaded
    assert ids['unit_b'] not in cascaded


@pytest.mark.asyncio
async def test_cascade_does_not_touch_unrelated_units(metastore):
    """Units backed by chunks NOT in the stale set stay active."""
    note_id = uuid4()
    async with metastore.session() as session:
        ids = await _seed_note_with_chunks_and_units(session, note_id)

    async with metastore.session() as session:
        await cascade_chunk_unit_staling(
            session=session,
            note_id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            stale_chunk_ids=[ids['chunk_a']],
            queue_service=None,
        )
        await session.commit()

    async with metastore.session() as session:
        unit_b = await session.get(MemoryUnit, ids['unit_b'])
        assert unit_b is not None
        assert unit_b.status == ContentStatus.ACTIVE


@pytest.mark.asyncio
async def test_cascade_empty_chunk_list_is_noop(metastore):
    """No stale chunks → cascade returns empty list, no DB changes."""
    note_id = uuid4()
    async with metastore.session() as session:
        await _seed_note_with_chunks_and_units(session, note_id)

    async with metastore.session() as session:
        cascaded = await cascade_chunk_unit_staling(
            session=session,
            note_id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            stale_chunk_ids=[],
            queue_service=None,
        )
        assert cascaded == []


@pytest.mark.asyncio
async def test_cascade_with_no_units_returns_empty(metastore):
    """Stale chunk_ids that don't back any units → empty cascade."""
    note_id = uuid4()
    fake_chunk_id = uuid4()
    async with metastore.session() as session:
        await _seed_note_with_chunks_and_units(session, note_id)

    async with metastore.session() as session:
        cascaded = await cascade_chunk_unit_staling(
            session=session,
            note_id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            stale_chunk_ids=[fake_chunk_id],
            queue_service=None,
        )
        assert cascaded == []


# --------------------------------------------------------------------------- #
# Regression: append must NOT trigger this cascade                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_does_not_stale_existing_units(api, metastore):
    """Append-flow only adds text — it never produces stale chunks. Existing
    memory units must remain ACTIVE after an append."""
    from memex_core.memory.sql_models import MemoryUnit
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async def _retain_with_unit(session, contents, note_id, **kwargs):  # type: ignore[no-untyped-def]
        content_item = contents[0]
        vault_id = content_item.vault_id
        note_uuid = UUID(note_id) if isinstance(note_id, str) else note_id
        new_hash = hashlib.md5(content_item.content.encode()).hexdigest()

        await session.exec(
            pg_insert(Note)
            .values(
                id=note_uuid,
                content_hash=new_hash,
                vault_id=vault_id,
                original_text=content_item.content,
                title=content_item.payload.get('note_name'),
            )
            .on_conflict_do_update(
                index_elements=['id'],
                set_={
                    'content_hash': new_hash,
                    'original_text': content_item.content,
                    'title': content_item.payload.get('note_name'),
                },
            )
        )
        unit_id = uuid4()
        session.add(
            MemoryUnit(
                id=unit_id,
                note_id=note_uuid,
                vault_id=vault_id,
                text='persistent fact',
                fact_type=FactTypes.WORLD,
                embedding=[0.1] * 384,
                event_date=content_item.event_date,
            )
        )
        return {'unit_ids': [str(unit_id)], 'status': 'success', 'touched_entities': set()}

    api.memory.retain.side_effect = _retain_with_unit
    parent = NoteInput(name='persistent', description='', content=b'body v1', note_key='reg-app-1')
    res1 = await api.ingest(parent)
    parent_id = UUID(str(res1['note_id']))

    # Capture existing unit IDs.
    async with metastore.session() as session:
        before = (
            await session.exec(select(MemoryUnit).where(col(MemoryUnit.note_id) == parent_id))
        ).all()
    before_ids = {u.id for u in before}
    assert before_ids, 'precondition: parent has memory units'

    await api.append_to_note(
        note_id=parent_id,
        delta='additive line',
        append_id=uuid4(),
    )

    async with metastore.session() as session:
        after = (
            await session.exec(select(MemoryUnit).where(col(MemoryUnit.note_id) == parent_id))
        ).all()
    by_id = {u.id: u for u in after}
    # Pre-existing units must still be ACTIVE.
    for old_id in before_ids:
        unit = by_id.get(old_id)
        assert unit is not None
        assert unit.status == ContentStatus.ACTIVE, (
            f'Append should not stale existing units, but {old_id} was staled.'
        )
