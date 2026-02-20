import pytest
from datetime import datetime, timezone
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.entity_resolver import EntityResolver
from memex_common.types import FactTypes
from memex_core.memory.sql_models import Entity, UnitEntity, EntityCooccurrence

# -- Fixtures --


@pytest.fixture
async def setup_extensions(session: AsyncSession):
    """Ensure pg_trgm is enabled for fuzzy matching."""
    await session.exec(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
    await session.commit()


@pytest.fixture
def resolver():
    return EntityResolver(resolution_threshold=0.4)


# -- Tests --


@pytest.mark.asyncio
async def test_resolve_batch_creates_new(session: AsyncSession, resolver, setup_extensions):
    """Verify that new entities are created correctly."""
    data = [
        {'text': 'New Entity A', 'event_date': datetime.now(timezone.utc)},
        {'text': 'New Entity B', 'event_date': datetime.now(timezone.utc)},
    ]

    ids = await resolver.resolve_entities_batch(session, data, datetime.now(timezone.utc))

    assert len(ids) == 2
    assert ids[0] != ids[1]

    # Verify DB
    e1 = await session.get(Entity, ids[0])
    e2 = await session.get(Entity, ids[1])
    assert e1 is not None
    assert e2 is not None

    assert e1.canonical_name == 'New Entity A'
    assert e1.mention_count == 1
    assert e2.canonical_name == 'New Entity B'


@pytest.mark.asyncio
async def test_resolve_batch_matches_existing(session: AsyncSession, resolver, setup_extensions):
    """Verify linking to existing entities."""
    # Setup existing
    e = Entity(
        canonical_name='Existing Entity',
        mention_count=1,
        last_seen=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    session.add(e)
    await session.commit()
    await session.refresh(e)
    original_id = str(e.id)

    # Resolve
    data = [{'text': 'Existing Entity', 'event_date': datetime.now(timezone.utc)}]
    ids = await resolver.resolve_entities_batch(session, data, datetime.now(timezone.utc))

    assert len(ids) == 1
    assert ids[0] == original_id

    # Verify Update
    await session.refresh(e)
    assert e.mention_count == 2
    assert e.last_seen > datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_resolve_batch_fuzzy_match(
    session: AsyncSession, resolver: EntityResolver, setup_extensions
):
    """Verify fuzzy matching with pg_trgm."""
    # Create canonical
    e = Entity(canonical_name='Elon Musk', mention_count=10)
    session.add(e)
    await session.commit()
    await session.refresh(e)

    # Resolve typo
    # "Elom Musk" is very close to "Elon Musk"
    data = [{'text': 'Elom Musk', 'event_date': datetime.now(timezone.utc)}]
    ids = await resolver.resolve_entities_batch(session, data, datetime.now(timezone.utc))

    assert len(ids) == 1
    assert ids[0] == str(e.id)


@pytest.mark.asyncio
async def test_resolve_batch_grouping(session: AsyncSession, resolver, setup_extensions):
    """Verify that duplicates in batch are grouped."""
    data = [
        {'text': 'Python', 'event_date': datetime.now(timezone.utc)},
        {'text': 'python', 'event_date': datetime.now(timezone.utc)},  # Lowercase
        {'text': 'PYTHON', 'event_date': datetime.now(timezone.utc)},  # Uppercase
    ]

    ids = await resolver.resolve_entities_batch(session, data, datetime.now(timezone.utc))

    assert len(ids) == 3
    # All should point to same ID
    assert ids[0] == ids[1] == ids[2]

    e = await session.get(Entity, ids[0])
    assert e is not None
    assert e.mention_count == 3


@pytest.mark.asyncio
async def test_link_units_to_entities(
    session: AsyncSession, resolver: EntityResolver, setup_extensions
):
    """Verify bulk linking and co-occurrence updates."""
    # Setup
    e1 = Entity(canonical_name='E1')
    e2 = Entity(canonical_name='E2')
    session.add(e1)
    session.add(e2)
    await session.commit()

    # Fake Unit IDs (doesn't check FK in this specific function logic, but DB might if FK exists)
    # UnitEntity has FK to MemoryUnit. So we MUST create MemoryUnits.
    from memex_core.memory.sql_models import MemoryUnit

    u1 = MemoryUnit(
        text='t1',
        embedding=[0.0] * 384,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
    )
    session.add(u1)
    await session.commit()

    u1_id = str(u1.id)
    e1_id = str(e1.id)
    e2_id = str(e2.id)

    pairs = [(u1_id, e1_id), (u1_id, e2_id)]

    await resolver.link_units_to_entities_batch(session, pairs)

    # Verify UnitEntity
    links = await session.exec(select(UnitEntity).where(UnitEntity.unit_id == u1.id))
    assert len(links.all()) == 2

    # Verify Cooccurrence
    # E1 and E2 appear in U1 -> Cooccurrence count 1
    # Note: IDs must be sorted in DB
    sorted_ids = sorted([e1.id, e2.id])
    cooc = await session.exec(
        select(EntityCooccurrence).where(
            EntityCooccurrence.entity_id_1 == sorted_ids[0],
            EntityCooccurrence.entity_id_2 == sorted_ids[1],
        )
    )
    res = cooc.first()
    assert res is not None
    assert res.cooccurrence_count == 1
