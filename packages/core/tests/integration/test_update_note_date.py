"""Integration tests for NoteService.update_note_date and entity cleanup on delete."""

import datetime as dt
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    Entity,
    EvidenceItem,
    MemoryUnit,
    MentalModel,
    Note,
    Observation,
    UnitEntity,
)
from memex_core.services.notes import await_background_tasks as await_notes_bg
from memex_core.services.stats import await_background_tasks as await_stats_bg


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_note_date_shifts_memory_units(api, metastore, session: AsyncSession):
    """Verify update_note_date shifts Note.publish_date and all MemoryUnit temporal fields."""
    old_date = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_date = dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    delta = new_date - old_date

    note_id = uuid.uuid4()
    unit_id_1 = uuid.uuid4()
    unit_id_2 = uuid.uuid4()

    # Create note with known publish_date
    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Test note for date update {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
        publish_date=old_date,
        doc_metadata={'publish_date': old_date.isoformat()},
    )
    session.add(note)

    # Create memory units with various temporal fields
    mentioned_at_1 = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    occurred_start_1 = dt.datetime(2024, 1, 10, 8, 0, 0, tzinfo=dt.timezone.utc)
    occurred_end_1 = dt.datetime(2024, 1, 12, 18, 0, 0, tzinfo=dt.timezone.utc)
    event_date_1 = occurred_start_1

    unit_1 = MemoryUnit(
        id=unit_id_1,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id,
        text=f'Fact with occurred dates {uuid.uuid4()}',
        fact_type=FactTypes.EVENT,
        embedding=[0.1] * 384,
        event_date=event_date_1,
        mentioned_at=mentioned_at_1,
        occurred_start=occurred_start_1,
        occurred_end=occurred_end_1,
    )
    session.add(unit_1)

    # Unit 2: no occurred_start/end (NULL)
    mentioned_at_2 = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    event_date_2 = mentioned_at_2

    unit_2 = MemoryUnit(
        id=unit_id_2,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id,
        text=f'Fact without occurred dates {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=event_date_2,
        mentioned_at=mentioned_at_2,
        occurred_start=None,
        occurred_end=None,
    )
    session.add(unit_2)
    await session.commit()

    # Execute update_note_date
    result = await api.update_note_date(note_id, new_date)

    assert result['note_id'] == str(note_id)
    assert result['units_updated'] == 2

    # Verify Note.publish_date updated
    async with metastore.session() as verify_session:
        doc = await verify_session.get(Note, note_id)
        assert doc is not None
        assert doc.publish_date == new_date
        assert doc.doc_metadata['publish_date'] == new_date.isoformat()

        # Verify unit 1: all temporal fields shifted
        u1 = await verify_session.get(MemoryUnit, unit_id_1)
        assert u1 is not None
        assert u1.event_date == event_date_1 + delta
        assert u1.mentioned_at == mentioned_at_1 + delta
        assert u1.occurred_start == occurred_start_1 + delta
        assert u1.occurred_end == occurred_end_1 + delta

        # Verify unit 2: NULL fields stay NULL, non-null shifted
        u2 = await verify_session.get(MemoryUnit, unit_id_2)
        assert u2 is not None
        assert u2.event_date == event_date_2 + delta
        assert u2.mentioned_at == mentioned_at_2 + delta
        assert u2.occurred_start is None
        assert u2.occurred_end is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_note_date_null_publish_date_falls_back_to_created_at(
    api, metastore, session: AsyncSession
):
    """Verify update_note_date uses created_at when publish_date is NULL."""
    note_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    created_at = dt.datetime(2024, 3, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    new_date = dt.datetime(2024, 9, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    delta = new_date - created_at

    # Create note without publish_date
    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note without publish_date {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
        publish_date=None,
        created_at=created_at,
    )
    session.add(note)

    unit = MemoryUnit(
        id=unit_id,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id,
        text=f'Fact {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=created_at,
        mentioned_at=created_at,
    )
    session.add(unit)
    await session.commit()

    result = await api.update_note_date(note_id, new_date)
    assert result['units_updated'] == 1

    async with metastore.session() as verify_session:
        doc = await verify_session.get(Note, note_id)
        assert doc is not None
        assert doc.publish_date == new_date

        u = await verify_session.get(MemoryUnit, unit_id)
        assert u is not None
        assert u.event_date == created_at + delta
        assert u.mentioned_at == created_at + delta


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_note_date_page_index_metadata(api, metastore, session: AsyncSession):
    """Verify update_note_date updates page_index metadata."""
    old_date = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_date = dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    note_id = uuid.uuid4()

    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note with page_index {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
        publish_date=old_date,
        page_index={'metadata': {'title': 'Test', 'publish_date': old_date.isoformat()}, 'toc': []},
    )
    session.add(note)
    await session.commit()

    await api.update_note_date(note_id, new_date)

    async with metastore.session() as verify_session:
        doc = await verify_session.get(Note, note_id)
        assert doc is not None
        assert doc.page_index['metadata']['publish_date'] == new_date.isoformat()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_note_date_no_memory_units(api, metastore, session: AsyncSession):
    """Verify update_note_date works when note has no memory units."""
    old_date = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_date = dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    note_id = uuid.uuid4()

    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note with no units {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
        publish_date=old_date,
    )
    session.add(note)
    await session.commit()

    result = await api.update_note_date(note_id, new_date)
    assert result['units_updated'] == 0

    async with metastore.session() as verify_session:
        doc = await verify_session.get(Note, note_id)
        assert doc is not None
        assert doc.publish_date == new_date


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_note_date_does_not_affect_other_notes(api, metastore, session: AsyncSession):
    """Verify update_note_date only shifts units belonging to the target note."""
    old_date = dt.datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_date = dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    note_id_target = uuid.uuid4()
    note_id_other = uuid.uuid4()
    unit_id_target = uuid.uuid4()
    unit_id_other = uuid.uuid4()
    other_event_date = dt.datetime(2024, 2, 1, 12, 0, 0, tzinfo=dt.timezone.utc)

    # Create two notes
    for nid in [note_id_target, note_id_other]:
        session.add(
            Note(
                id=nid,
                vault_id=GLOBAL_VAULT_ID,
                original_text=f'Note {nid} {uuid.uuid4()}',
                content_hash=f'hash_{uuid.uuid4()}',
                publish_date=old_date,
            )
        )

    session.add(
        MemoryUnit(
            id=unit_id_target,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_target,
            text=f'Target fact {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=old_date,
            mentioned_at=old_date,
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id_other,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_other,
            text=f'Other fact {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=other_event_date,
            mentioned_at=other_event_date,
        )
    )
    await session.commit()

    await api.update_note_date(note_id_target, new_date)

    # Other note's unit should be UNCHANGED
    async with metastore.session() as verify_session:
        u_other = await verify_session.get(MemoryUnit, unit_id_other)
        assert u_other is not None
        assert u_other.event_date == other_event_date
        assert u_other.mentioned_at == other_event_date


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_removes_orphaned_entities(api, metastore, session: AsyncSession):
    """Verify delete_note removes entities with no remaining links."""
    note_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    entity_id_orphan = uuid.uuid4()

    # Create note
    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Test note for entity cleanup {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
    )
    session.add(note)

    # Create memory unit
    unit = MemoryUnit(
        id=unit_id,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id,
        text=f'Fact about entity {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
    )
    session.add(unit)

    # Create entity that will become orphaned
    entity_orphan = Entity(
        id=entity_id_orphan,
        canonical_name=f'Orphan Entity {uuid.uuid4()}',
        mention_count=1,
    )
    session.add(entity_orphan)
    await session.flush()

    # Link entity to unit
    ue = UnitEntity(
        unit_id=unit_id,
        entity_id=entity_id_orphan,
        vault_id=GLOBAL_VAULT_ID,
    )
    session.add(ue)
    await session.commit()

    # Delete the note
    result = await api.delete_note(note_id)
    assert result is True
    await await_notes_bg()

    # Verify entity was deleted
    async with metastore.session() as verify_session:
        entity = await verify_session.get(Entity, entity_id_orphan)
        assert entity is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_updates_mention_count_for_shared_entities(
    api, metastore, session: AsyncSession
):
    """Verify delete_note updates mention_count for entities still referenced by other notes."""
    note_id_1 = uuid.uuid4()
    note_id_2 = uuid.uuid4()
    unit_id_1 = uuid.uuid4()
    unit_id_2 = uuid.uuid4()
    entity_id_shared = uuid.uuid4()

    # Create two notes
    note_1 = Note(
        id=note_id_1,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note 1 {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
    )
    note_2 = Note(
        id=note_id_2,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note 2 {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
    )
    session.add(note_1)
    session.add(note_2)

    # Create memory units for both notes
    unit_1 = MemoryUnit(
        id=unit_id_1,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id_1,
        text=f'Fact from note 1 {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
    )
    unit_2 = MemoryUnit(
        id=unit_id_2,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id_2,
        text=f'Fact from note 2 {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
    )
    session.add(unit_1)
    session.add(unit_2)

    # Create shared entity with mention_count=5 (artificially high)
    entity_shared = Entity(
        id=entity_id_shared,
        canonical_name=f'Shared Entity {uuid.uuid4()}',
        mention_count=5,
    )
    session.add(entity_shared)
    await session.flush()

    # Link entity to both units
    ue_1 = UnitEntity(
        unit_id=unit_id_1,
        entity_id=entity_id_shared,
        vault_id=GLOBAL_VAULT_ID,
    )
    ue_2 = UnitEntity(
        unit_id=unit_id_2,
        entity_id=entity_id_shared,
        vault_id=GLOBAL_VAULT_ID,
    )
    session.add(ue_1)
    session.add(ue_2)
    await session.commit()

    # Delete note 1
    result = await api.delete_note(note_id_1)
    assert result is True
    await await_notes_bg()

    # Verify entity still exists with updated mention_count = 1
    async with metastore.session() as verify_session:
        entity = await verify_session.get(Entity, entity_id_shared)
        assert entity is not None
        assert entity.mention_count == 1

        # Verify note 2 and its unit are unaffected
        note2 = await verify_session.get(Note, note_id_2)
        assert note2 is not None
        u2 = await verify_session.get(MemoryUnit, unit_id_2)
        assert u2 is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_removes_mental_models_for_orphaned_entities(
    api, metastore, session: AsyncSession
):
    """Verify delete_note removes mental models when their entity becomes orphaned."""
    note_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    mental_model_id = uuid.uuid4()

    note = Note(
        id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Note with mental model entity {uuid.uuid4()}',
        content_hash=f'hash_{uuid.uuid4()}',
    )
    session.add(note)

    unit = MemoryUnit(
        id=unit_id,
        vault_id=GLOBAL_VAULT_ID,
        note_id=note_id,
        text=f'Fact about entity {uuid.uuid4()}',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
    )
    session.add(unit)

    entity = Entity(
        id=entity_id,
        canonical_name=f'Entity With MM {uuid.uuid4()}',
        mention_count=1,
    )
    session.add(entity)

    mental_model = MentalModel(
        id=mental_model_id,
        vault_id=GLOBAL_VAULT_ID,
        entity_id=entity_id,
        name=entity.canonical_name,
    )
    session.add(mental_model)
    await session.flush()

    ue = UnitEntity(
        unit_id=unit_id,
        entity_id=entity_id,
        vault_id=GLOBAL_VAULT_ID,
    )
    session.add(ue)
    await session.commit()

    # Verify mental model exists before deletion
    async with metastore.session() as pre_session:
        mm = await pre_session.get(MentalModel, mental_model_id)
        assert mm is not None

    # Delete the note
    await api.delete_note(note_id)
    await await_notes_bg()

    # Verify both entity AND mental model are deleted
    async with metastore.session() as verify_session:
        assert await verify_session.get(Entity, entity_id) is None
        assert await verify_session.get(MentalModel, mental_model_id) is None


def _make_observation_dict(title: str, unit_ids: list[uuid.UUID]) -> dict:
    """Helper to build a serialized Observation with evidence citing given unit_ids."""
    obs = Observation(
        title=title,
        content=f'Content for {title}',
        evidence=[
            EvidenceItem(
                memory_id=uid,
                quote=f'quote from {uid}',
                relevance=1.0,
            )
            for uid in unit_ids
        ],
    )
    return obs.model_dump(mode='json')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_prunes_mental_model_observations(api, metastore, session: AsyncSession):
    """After deleting note_1 (shared entity): MM survives with only note_2 obs, embedding=None."""
    note_id_1 = uuid.uuid4()
    note_id_2 = uuid.uuid4()
    unit_id_1 = uuid.uuid4()
    unit_id_2 = uuid.uuid4()
    entity_id = uuid.uuid4()
    mm_id = uuid.uuid4()

    for nid in [note_id_1, note_id_2]:
        session.add(
            Note(
                id=nid,
                vault_id=GLOBAL_VAULT_ID,
                original_text=f'Note {nid} {uuid.uuid4()}',
                content_hash=f'hash_{uuid.uuid4()}',
            )
        )

    session.add(
        MemoryUnit(
            id=unit_id_1,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_1,
            text=f'Fact 1 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id_2,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_2,
            text=f'Fact 2 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )

    entity = Entity(id=entity_id, canonical_name=f'Shared {uuid.uuid4()}', mention_count=2)
    session.add(entity)
    await session.flush()

    for uid in [unit_id_1, unit_id_2]:
        session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))

    # MM with observations citing both units
    session.add(
        MentalModel(
            id=mm_id,
            vault_id=GLOBAL_VAULT_ID,
            entity_id=entity_id,
            name=entity.canonical_name,
            embedding=[0.2] * 384,
            observations=[
                _make_observation_dict('obs from note1', [unit_id_1]),
                _make_observation_dict('obs from note2', [unit_id_2]),
            ],
        )
    )
    await session.commit()

    await api.delete_note(note_id_1)
    await await_notes_bg()

    async with metastore.session() as vs:
        mm = await vs.get(MentalModel, mm_id)
        assert mm is not None
        assert len(mm.observations) == 1
        obs = Observation(**mm.observations[0])
        assert obs.title == 'obs from note2'
        assert obs.evidence[0].memory_id == unit_id_2
        assert mm.embedding is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_removes_empty_mental_model(api, metastore, session: AsyncSession):
    """If all MM obs cite only the deleted note's units, the MM is deleted; entity survives."""
    note_id_1 = uuid.uuid4()
    note_id_2 = uuid.uuid4()
    unit_id_1 = uuid.uuid4()
    unit_id_2 = uuid.uuid4()
    entity_id = uuid.uuid4()
    mm_id = uuid.uuid4()

    for nid in [note_id_1, note_id_2]:
        session.add(
            Note(
                id=nid,
                vault_id=GLOBAL_VAULT_ID,
                original_text=f'Note {nid} {uuid.uuid4()}',
                content_hash=f'hash_{uuid.uuid4()}',
            )
        )

    session.add(
        MemoryUnit(
            id=unit_id_1,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_1,
            text=f'Fact 1 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id_2,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id_2,
            text=f'Fact 2 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )

    entity = Entity(id=entity_id, canonical_name=f'Shared {uuid.uuid4()}', mention_count=2)
    session.add(entity)
    await session.flush()

    for uid in [unit_id_1, unit_id_2]:
        session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))

    # MM with observations citing ONLY note_1's unit
    session.add(
        MentalModel(
            id=mm_id,
            vault_id=GLOBAL_VAULT_ID,
            entity_id=entity_id,
            name=entity.canonical_name,
            observations=[_make_observation_dict('obs only from note1', [unit_id_1])],
        )
    )
    await session.commit()

    await api.delete_note(note_id_1)
    await await_notes_bg()

    async with metastore.session() as vs:
        # Entity still exists (note_2's unit still references it)
        e = await vs.get(Entity, entity_id)
        assert e is not None
        assert e.mention_count == 1
        # MM was deleted because all observations lost their evidence
        mm = await vs.get(MentalModel, mm_id)
        assert mm is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_memory_unit_prunes_observations(api, metastore, session: AsyncSession):
    """Deleting one of two units: MM survives with pruned observations."""
    note_id = uuid.uuid4()
    unit_id_1 = uuid.uuid4()
    unit_id_2 = uuid.uuid4()
    entity_id = uuid.uuid4()
    mm_id = uuid.uuid4()

    session.add(
        Note(
            id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            original_text=f'Note {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id_1,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            text=f'Fact 1 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id_2,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            text=f'Fact 2 {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )

    entity = Entity(id=entity_id, canonical_name=f'Entity {uuid.uuid4()}', mention_count=2)
    session.add(entity)
    await session.flush()

    for uid in [unit_id_1, unit_id_2]:
        session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))

    session.add(
        MentalModel(
            id=mm_id,
            vault_id=GLOBAL_VAULT_ID,
            entity_id=entity_id,
            name=entity.canonical_name,
            embedding=[0.2] * 384,
            observations=[
                _make_observation_dict('obs from unit1', [unit_id_1]),
                _make_observation_dict('obs from unit2', [unit_id_2]),
            ],
        )
    )
    await session.commit()

    await api.delete_memory_unit(unit_id_1)
    await await_stats_bg()

    async with metastore.session() as vs:
        mm = await vs.get(MentalModel, mm_id)
        assert mm is not None
        assert len(mm.observations) == 1
        obs = Observation(**mm.observations[0])
        assert obs.title == 'obs from unit2'
        assert mm.embedding is None

        e = await vs.get(Entity, entity_id)
        assert e is not None
        assert e.mention_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_memory_unit_entity_cleanup(api, metastore, session: AsyncSession):
    """Deleting the only unit: entity + MM both deleted."""
    note_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    mm_id = uuid.uuid4()

    session.add(
        Note(
            id=note_id,
            vault_id=GLOBAL_VAULT_ID,
            original_text=f'Note {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
    )
    session.add(
        MemoryUnit(
            id=unit_id,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            text=f'Fact {uuid.uuid4()}',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            event_date=dt.datetime.now(dt.timezone.utc),
        )
    )

    entity = Entity(id=entity_id, canonical_name=f'Entity {uuid.uuid4()}', mention_count=1)
    session.add(entity)
    await session.flush()

    session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))

    session.add(
        MentalModel(
            id=mm_id,
            vault_id=GLOBAL_VAULT_ID,
            entity_id=entity_id,
            name=entity.canonical_name,
            observations=[_make_observation_dict('obs', [unit_id])],
        )
    )
    await session.commit()

    await api.delete_memory_unit(unit_id)
    await await_stats_bg()

    async with metastore.session() as vs:
        assert await vs.get(Entity, entity_id) is None
        assert await vs.get(MentalModel, mm_id) is None
