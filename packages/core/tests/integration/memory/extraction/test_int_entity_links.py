import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit, MemoryLink, Entity, Document
from memex_core.memory.extraction import entity_links
from memex_core.memory.extraction.models import EntityLink


async def create_document(session: AsyncSession) -> Document:
    doc = Document(id=uuid4(), original_text='Test Doc')
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


async def create_unit(
    session: AsyncSession,
    doc_id: UUID,
    text: str = 'Test Unit',
    event_date: datetime | None = None,
    embedding: list[float] | None = None,
) -> MemoryUnit:
    if event_date is None:
        event_date = datetime.now(timezone.utc)

    if embedding is None:
        embedding = [0.0] * 384

    unit = MemoryUnit(
        id=uuid4(),
        document_id=doc_id,
        text=text,
        event_date=event_date,
        embedding=embedding,
        fact_type=FactTypes.WORLD,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)
    return unit


async def create_entity(session: AsyncSession, name: str) -> Entity:
    entity = Entity(id=uuid4(), canonical_name=name)
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    return entity


@pytest.mark.asyncio
async def test_int_create_temporal_links_batch_per_fact(session: AsyncSession):
    # Arrange
    doc = await create_document(session)
    base_time = datetime.now(timezone.utc)

    unit1 = await create_unit(session, doc.id, 'Unit 1', event_date=base_time)
    unit2 = await create_unit(session, doc.id, 'Unit 2', event_date=base_time + timedelta(hours=1))
    await create_unit(
        session, doc.id, 'Unit 3', event_date=base_time + timedelta(hours=48)
    )  # Far away

    # Act: Link Unit 2 back to whatever is close (Unit 1)
    # Note: The function queries for candidates internally based on Unit 2's date
    count = await entity_links.create_temporal_links_batch_per_fact(session, [str(unit2.id)])

    # Assert
    assert count == 1

    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 1
    assert links[0].from_unit_id == unit2.id
    assert links[0].to_unit_id == unit1.id
    assert links[0].link_type == 'temporal'


@pytest.mark.asyncio
async def test_int_create_semantic_links_batch(session: AsyncSession):
    # Arrange
    doc = await create_document(session)
    # Unit 1: [1, 0, ..., 0]
    emb1 = [0.0] * 384
    emb1[0] = 1.0
    unit1 = await create_unit(session, doc.id, 'Existing Unit', embedding=emb1)

    # Unit 2 (New): Similar embedding
    emb2 = [0.0] * 384
    emb2[0] = 0.9  # High similarity
    unit2 = await create_unit(session, doc.id, 'New Unit', embedding=emb2)
    u2_id = unit2.id

    # Act
    count = await entity_links.create_semantic_links_batch(
        session, unit_ids=[str(u2_id)], embeddings=[emb2], threshold=0.8
    )

    # Assert
    assert count == 1
    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 1
    assert links[0].from_unit_id == u2_id
    assert links[0].to_unit_id == unit1.id
    assert links[0].link_type == 'semantic'


@pytest.mark.asyncio
async def test_int_create_causal_links_batch(session: AsyncSession):
    # Arrange
    doc = await create_document(session)
    unit1 = await create_unit(session, doc.id, 'Cause')
    unit2 = await create_unit(session, doc.id, 'Effect')

    relations = [
        [{'target_fact_index': 1, 'relation_type': 'causes', 'strength': 0.9}],  # unit1 -> unit2
        [],  # unit2
    ]

    # Act
    count = await entity_links.create_causal_links_batch(
        session, unit_ids=[str(unit1.id), str(unit2.id)], causal_relations_per_fact=relations
    )

    # Assert
    assert count == 1
    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 1
    assert links[0].from_unit_id == unit1.id
    assert links[0].to_unit_id == unit2.id
    assert links[0].link_type == 'causes'


@pytest.mark.asyncio
async def test_int_insert_entity_links_batch(session: AsyncSession):
    # Arrange
    doc = await create_document(session)
    unit1 = await create_unit(session, doc.id, 'Unit 1')
    unit2 = await create_unit(session, doc.id, 'Unit 2')
    entity = await create_entity(session, 'Concept X')

    link = EntityLink(
        from_unit_id=unit1.id,
        to_unit_id=unit2.id,
        entity_id=entity.id,
        link_type='entity',
        weight=1.0,
    )

    # Act
    await entity_links.insert_entity_links_batch(session, [link])

    # Assert
    links = (await session.exec(select(MemoryLink))).all()
    assert len(links) == 1
    assert links[0].entity_id == entity.id
    assert links[0].link_type == 'entity'
