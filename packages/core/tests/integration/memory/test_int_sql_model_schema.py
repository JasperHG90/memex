from typing import cast

import pytest
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, col
from uuid import uuid4
from datetime import datetime, timezone

# Import your models
from memex_core.memory.sql_models import (
    Note,
    MemoryUnit,
    Chunk,
    Entity,
    EntityCooccurrence,
    UnitEntity,
    MemoryLink,
)
from memex_common.types import FactTypes


# --- 1. HAPPY PATH & RELATIONSHIPS ---
@pytest.mark.asyncio
async def test_create_document_and_units(session: AsyncSession):
    """
    Test that we can save a Note and associated MemoryUnits,
    and that relationships (back_populates) work.
    """
    # 1. Create a Note
    doc_id = uuid4()
    doc = Note(
        id=doc_id,
        original_text='Elon Musk bought Twitter.',
        content_hash='abc123hash',
        assets=['images/logo.png', 'data/table.csv'],  # Testing the ARRAY column
    )
    session.add(doc)
    await session.commit()

    # 2. Create a MemoryUnit linked to that Note
    unit = MemoryUnit(
        bank_id='user_1',
        note_id=doc_id,  # Link via ID
        fact_type=FactTypes.WORLD,
        text='Elon Musk bought Twitter.',
        event_date=datetime.now(),
    )
    session.add(unit)
    await session.commit()

    # 3. Verify Data & Relationship loading
    # Refresh doc to load the relationship
    await session.refresh(doc, ['memory_units'])

    assert len(doc.memory_units) == 1
    assert doc.memory_units[0].text == 'Elon Musk bought Twitter.'
    assert doc.memory_units[0].note_id == doc_id

    # Verify Array storage
    assert doc.assets == ['images/logo.png', 'data/table.csv']


@pytest.mark.asyncio
async def test_create_document_and_chunks(session: AsyncSession):
    """
    Test that we can save a Note and associated Chunks,
    and that relationships (back_populates) work.
    """
    # 1. Create a Note
    doc_id = uuid4()
    doc = Note(
        id=doc_id,
        original_text='This is a long document that will be chunked.',
        content_hash='chunktest123',
    )
    session.add(doc)
    await session.commit()

    # 2. Create Chunks linked to that Note
    chunk1 = Chunk(
        note_id=doc_id,
        text='This is a long document',
        chunk_index=0,
        content_hash='chunk_hash_1',
    )
    chunk2 = Chunk(
        note_id=doc_id,
        text='that will be chunked.',
        chunk_index=1,
        content_hash='chunk_hash_2',
    )
    session.add(chunk1)
    session.add(chunk2)
    await session.commit()

    # 3. Verify Data & Relationship loading
    await session.refresh(doc, ['chunks'])

    assert len(doc.chunks) == 2
    # Sort by chunk_index to verify order
    sorted_chunks = sorted(doc.chunks, key=lambda x: x.chunk_index)
    assert sorted_chunks[0].text == 'This is a long document'
    assert sorted_chunks[1].text == 'that will be chunked.'
    assert sorted_chunks[0].note_id == doc_id


@pytest.mark.asyncio
async def test_chunk_cascade_delete(session: AsyncSession):
    """
    Test that deleting a Note automatically deletes its Chunks.
    """
    # Setup: Create Doc + Chunk
    doc = Note(id=uuid4())
    session.add(doc)
    await session.commit()

    chunk = Chunk(
        note_id=doc.id,
        text='Temporary chunk',
        chunk_index=0,
        content_hash='temp_chunk_hash',
    )
    session.add(chunk)
    await session.commit()

    # Verify chunk exists
    result = await session.get(Chunk, chunk.id)
    assert result is not None

    # ACT: Delete the Note
    await session.delete(doc)
    await session.commit()

    # ASSERT: The Chunk should be gone (Cascade)
    result = await session.get(Chunk, chunk.id)
    assert result is None


# --- 2. CASCADE DELETE TEST ---
@pytest.mark.asyncio
async def test_cascade_delete(session: AsyncSession):
    """
    Test that deleting a Note automatically deletes its MemoryUnits.
    """
    # Setup: Create Doc + Unit
    doc = Note(id=uuid4())
    session.add(doc)
    await session.commit()

    unit = MemoryUnit(
        note_id=doc.id,
        text='Temporary thought',
        embedding=[0.0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    session.add(unit)
    await session.commit()

    # Verify unit exists
    result = await session.get(MemoryUnit, unit.id)
    assert result is not None

    # ACT: Delete the Note
    await session.delete(doc)
    await session.commit()

    # ASSERT: The Unit should be gone (Cascade)
    result = await session.get(MemoryUnit, unit.id)
    assert result is None


# --- 3. CHECK CONSTRAINT TEST ---
@pytest.mark.asyncio
async def test_check_constraints_negative_beta(session: AsyncSession):
    """
    Test that the DB rejects negative confidence_beta values.
    """
    unit = MemoryUnit(
        text='Invalid Confidence',
        embedding=[0.0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
        # INVALID: This violates confidence_beta >= 0.0
        confidence_alpha=1.0,
        confidence_beta=-5.0,
    )
    session.add(unit)

    # Expect an IntegrityError (Database rejected the row)
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.commit()


# --- 4. PGVECTOR SEARCH TEST ---
@pytest.mark.asyncio
async def test_vector_search(session: AsyncSession):
    """
    Test that we can insert vectors and query by cosine distance.
    """
    # Insert 3 units with different vectors
    # Vec A: [1, 0, 0...]
    vec_a = [1.0] + [0.0] * 383
    # Vec B: [0, 1, 0...] (Orthogonal to A)
    vec_b = [0.0, 1.0] + [0.0] * 382

    unit_a = MemoryUnit(text='A', embedding=vec_a, event_date=datetime.now(timezone.utc))
    unit_b = MemoryUnit(text='B', embedding=vec_b, event_date=datetime.now(timezone.utc))

    session.add(unit_a)
    session.add(unit_b)
    await session.commit()

    # Query: Find neighbor closest to Vec A
    # We expect unit_a to be first (distance 0), unit_b to be second
    stmt = (
        select(MemoryUnit)
        .order_by(col(MemoryUnit.embedding).cosine_distance(vec_a))  # type: ignore
        .limit(1)
    )
    result = await session.exec(stmt)
    closest = cast(MemoryUnit, result.first())

    assert closest.text == 'A'


@pytest.mark.asyncio
async def test_entity_and_unit_association(session: AsyncSession):
    """
    Test creating an Entity, linking it to a MemoryUnit via UnitEntity,
    and verifying cascading deletes.
    """
    # 1. Setup: Doc + Unit
    doc = Note(id=uuid4(), original_text='Context')
    session.add(doc)
    await session.commit()

    unit = MemoryUnit(
        note_id=doc.id,
        text='Elon Musk owns Tesla.',
        embedding=[0.1] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    session.add(unit)

    # 2. Create Entity
    entity = Entity(canonical_name='Elon Musk', entity_metadata={'role': 'CEO'})
    session.add(entity)
    await session.commit()

    # 3. Link them (UnitEntity)
    association = UnitEntity(unit_id=unit.id, entity_id=entity.id)
    session.add(association)
    await session.commit()

    # 4. Verify Relationships
    # Reload unit and check relations
    await session.refresh(unit, ['unit_entities'])
    assert len(unit.unit_entities) == 1
    assert unit.unit_entities[0].entity_id == entity.id

    # Reload entity and check relations
    await session.refresh(entity, ['unit_entities'])
    assert len(entity.unit_entities) == 1
    assert entity.unit_entities[0].unit_id == unit.id

    # 5. Test Cascade: Delete Entity -> Association should be gone
    await session.delete(entity)
    await session.commit()

    # Check directly in the join table
    result = await session.get(UnitEntity, (unit.id, entity.id))
    assert result is None


@pytest.mark.asyncio
async def test_entity_cooccurrence_constraint(session: AsyncSession):
    """
    Test EntityCooccurrence enforces entity_id_1 < entity_id_2.
    """
    # 1. Create two entities
    e1 = Entity(canonical_name='Apple')
    e2 = Entity(canonical_name='Steve Jobs')
    session.add(e1)
    session.add(e2)
    await session.commit()

    # Sort IDs to determine valid order
    id_min, id_max = sorted([e1.id, e2.id])

    # 2. HAPPY PATH: Insert in correct order
    cooc = EntityCooccurrence(entity_id_1=id_min, entity_id_2=id_max, cooccurrence_count=5)
    session.add(cooc)
    await session.commit()

    # Verify load
    await session.refresh(cooc)
    assert cooc.cooccurrence_count == 5

    # 3. FAILURE PATH: Insert in wrong order
    # We must start a new transaction context or nested savepoint because the previous commit closed the transaction
    bad_cooc = EntityCooccurrence(
        entity_id_1=id_max,  # Wrong
        entity_id_2=id_min,  # Wrong
    )
    session.add(bad_cooc)

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.commit()


@pytest.mark.asyncio
async def test_memory_links_and_constraints(session: AsyncSession):
    """
    Test linking two units, valid link_types, and weight constraints.
    """
    # 1. Setup Units
    doc = Note(id=uuid4())
    session.add(doc)
    await session.commit()

    u_from = MemoryUnit(
        note_id=doc.id,
        text='Cause',
        embedding=[0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    u_to = MemoryUnit(
        note_id=doc.id,
        text='Effect',
        embedding=[0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    session.add(u_from)
    session.add(u_to)
    await session.commit()

    # 2. HAPPY PATH: Create a valid link
    link = MemoryLink(
        from_unit_id=u_from.id,
        to_unit_id=u_to.id,
        link_type='causes',  # Valid type
        weight=0.9,
    )
    session.add(link)
    await session.commit()

    # Verify relationships
    await session.refresh(u_from, ['outgoing_links'])
    assert len(u_from.outgoing_links) == 1
    assert u_from.outgoing_links[0].to_unit_id == u_to.id

    u_from_id = u_from.id
    u_to_id = u_to.id

    # 3. FAILURE PATH: Invalid Link Type
    bad_link_type = MemoryLink(
        from_unit_id=u_to.id,
        to_unit_id=u_from.id,
        link_type='magical_connection',  # INVALID
        weight=0.5,
    )
    session.add(bad_link_type)
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.commit()

    # Rollback session to clear error state
    await session.rollback()

    # 4. FAILURE PATH: Invalid Weight
    bad_weight = MemoryLink(
        from_unit_id=u_to_id,
        to_unit_id=u_from_id,
        link_type='temporal',
        weight=1.5,  # INVALID (must be <= 1.0)
    )
    session.add(bad_weight)
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.commit()


@pytest.mark.asyncio
async def test_memory_link_with_entity_cascade(session: AsyncSession):
    """
    Test that a MemoryLink can have an Entity, and deleting the Entity
    deletes the Link (Cascade).
    """
    # Setup
    doc = Note(id=uuid4())
    session.add(doc)
    await session.commit()

    u1 = MemoryUnit(
        note_id=doc.id,
        text='A',
        embedding=[0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    u2 = MemoryUnit(
        note_id=doc.id,
        text='B',
        embedding=[0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    entity = Entity(canonical_name='Reference')

    session.add(u1)
    session.add(u2)
    session.add(entity)
    await session.commit()

    # Create Link attached to Entity
    link = MemoryLink(
        from_unit_id=u1.id,
        to_unit_id=u2.id,
        link_type='entity',
        entity_id=entity.id,  # <-- Linked here
    )
    session.add(link)
    await session.commit()

    # Verify link exists
    # Note: MemoryLink has a composite PK (from, to, type, entity_id).
    # Retrieving by PK is verbose, so we just query it.
    stmt = select(MemoryLink).where(MemoryLink.link_type == 'entity')
    result = await session.exec(stmt)
    assert result.first() is not None

    # ACT: Delete Entity
    await session.delete(entity)
    await session.commit()

    # ASSERT: Link should be gone (Cascade)
    result = await session.exec(stmt)
    assert result.first() is None
