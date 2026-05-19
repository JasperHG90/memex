import pytest
import json
from uuid import UUID
from httpx import AsyncClient, ASGITransport
from memex_core.server import app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_stats_counts(api, metastore, init_global_vault):
    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/stats/counts')
        # This should fail because the endpoint is not implemented yet
        assert response.status_code == 200
        data = response.json()
        assert 'memories' in data
        assert 'entities' in data
        assert 'reflection_queue' in data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_entities_streaming(api, metastore, init_global_vault):
    await api.initialize()
    app.state.api = api

    # Ingest some data to have entities
    from memex_core.memory.sql_models import Entity

    # Mocking ingestion might be complex, let's just insert entities directly for this test
    async with metastore.session() as session:
        e1 = Entity(canonical_name='Jasper Ginn', mention_count=10, retrieval_count=5)
        e2 = Entity(canonical_name='Python', mention_count=20, retrieval_count=2)
        session.add(e1)
        session.add(e2)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        async with ac.stream('GET', '/api/v1/entities?limit=10') as response:
            assert response.status_code == 200
            content = []
            async for line in response.aiter_lines():
                if line:
                    content.append(json.loads(line))

            assert len(content) >= 2
            # Check for ranking (mention_count + retrieval_count)
            # Python: 20+2=22, Jasper: 10+5=15
            assert content[0]['name'] == 'Python'
            assert content[1]['name'] == 'Jasper Ginn'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_entity_mentions(api, metastore, init_global_vault):
    await api.initialize()
    app.state.api = api

    from memex_core.memory.sql_models import Entity, Note, MemoryUnit, UnitEntity
    from memex_common.config import GLOBAL_VAULT_ID
    import datetime

    async with metastore.session() as session:
        e1 = Entity(canonical_name='Target')
        d1 = Note(
            id=UUID('00000000-0000-0000-0000-000000000001'),
            vault_id=GLOBAL_VAULT_ID,
            original_text='Test',
        )
        session.add(e1)
        session.add(d1)
        await session.commit()
        await session.refresh(e1)

        u1 = MemoryUnit(
            text='Target mentioned here',
            vault_id=GLOBAL_VAULT_ID,
            note_id=d1.id,
            embedding=[0.1] * 384,
            event_date=datetime.datetime.now(datetime.timezone.utc),
        )
        session.add(u1)
        await session.commit()
        await session.refresh(u1)

        ue = UnitEntity(unit_id=u1.id, entity_id=e1.id, vault_id=GLOBAL_VAULT_ID)
        session.add(ue)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get(f'/api/v1/entities/{e1.id}/mentions')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['unit']['text'] == 'Target mentioned here'
        assert 'note' in data[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_entity_mentions_filters_default_to_active(api, metastore, init_global_vault):
    """V4: mentions endpoint hides stale / superseded / deprioritized units by default
    and surfaces them when include_* flags are set."""
    await api.initialize()
    app.state.api = api

    from memex_common.config import GLOBAL_VAULT_ID
    from memex_core.memory.sql_models import ContentStatus, Entity, MemoryUnit, Note, UnitEntity
    import datetime

    async with metastore.session() as session:
        ent = Entity(canonical_name='V4Target')
        note = Note(
            id=UUID('00000000-0000-0000-0000-000000000042'),
            vault_id=GLOBAL_VAULT_ID,
            original_text='V4',
        )
        session.add_all([ent, note])
        await session.commit()
        await session.refresh(ent)

        now = datetime.datetime.now(datetime.timezone.utc)
        active = MemoryUnit(
            text='active fact',
            vault_id=GLOBAL_VAULT_ID,
            note_id=note.id,
            embedding=[0.1] * 384,
            event_date=now,
            status=ContentStatus.ACTIVE,
            is_deprioritized=False,
            confidence=0.9,
        )
        deprio = MemoryUnit(
            text='deprioritized fact',
            vault_id=GLOBAL_VAULT_ID,
            note_id=note.id,
            embedding=[0.1] * 384,
            event_date=now,
            status=ContentStatus.ACTIVE,
            is_deprioritized=True,
            confidence=0.9,
        )
        stale = MemoryUnit(
            text='stale fact',
            vault_id=GLOBAL_VAULT_ID,
            note_id=note.id,
            embedding=[0.1] * 384,
            event_date=now,
            status=ContentStatus.STALE,
            is_deprioritized=False,
            confidence=0.9,
        )
        superseded = MemoryUnit(
            text='superseded fact',
            vault_id=GLOBAL_VAULT_ID,
            note_id=note.id,
            embedding=[0.1] * 384,
            event_date=now,
            status=ContentStatus.ACTIVE,
            is_deprioritized=False,
            confidence=0.05,
        )
        session.add_all([active, deprio, stale, superseded])
        await session.commit()
        for u in (active, deprio, stale, superseded):
            await session.refresh(u)
            session.add(UnitEntity(unit_id=u.id, entity_id=ent.id, vault_id=GLOBAL_VAULT_ID))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        default = await ac.get(f'/api/v1/entities/{ent.id}/mentions')
        assert default.status_code == 200
        default_texts = {
            json.loads(line)['unit']['text'] for line in default.text.splitlines() if line.strip()
        }
        assert default_texts == {'active fact'}

        widened = await ac.get(
            f'/api/v1/entities/{ent.id}/mentions',
            params={
                'include_stale': 'true',
                'include_superseded': 'true',
                'include_deprioritized': 'true',
            },
        )
        assert widened.status_code == 200
        widened_texts = {
            json.loads(line)['unit']['text'] for line in widened.text.splitlines() if line.strip()
        }
        assert widened_texts == {
            'active fact',
            'deprioritized fact',
            'stale fact',
            'superseded fact',
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_bulk_cooccurrences(api, metastore, init_global_vault):
    await api.initialize()
    app.state.api = api

    from memex_core.memory.sql_models import Entity, EntityCooccurrence
    from memex_common.config import GLOBAL_VAULT_ID

    async with metastore.session() as session:
        e1 = Entity(canonical_name='X')
        e2 = Entity(canonical_name='Y')
        session.add(e1)
        session.add(e2)
        await session.commit()
        await session.refresh(e1)
        await session.refresh(e2)

        co = EntityCooccurrence(
            entity_id_1=min(e1.id, e2.id),
            entity_id_2=max(e1.id, e2.id),
            cooccurrence_count=10,
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(co)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get(f'/api/v1/cooccurrences?ids={e1.id},{e2.id}')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['cooccurrence_count'] == 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_entity_cooccurrences_enriched(api, metastore, init_global_vault):
    """Entity cooccurrences endpoint returns entity names and types inline."""
    await api.initialize()
    app.state.api = api

    from memex_core.memory.sql_models import Entity, EntityCooccurrence
    from memex_common.config import GLOBAL_VAULT_ID

    async with metastore.session() as session:
        e1 = Entity(canonical_name='PostgreSQL', entity_type='Technology')
        e2 = Entity(canonical_name='Memex', entity_type='Software')
        e3 = Entity(canonical_name='FastAPI', entity_type='Framework')
        session.add_all([e1, e2, e3])
        await session.commit()
        await session.refresh(e1)
        await session.refresh(e2)
        await session.refresh(e3)

        co1 = EntityCooccurrence(
            entity_id_1=min(e1.id, e2.id),
            entity_id_2=max(e1.id, e2.id),
            cooccurrence_count=8,
            vault_id=GLOBAL_VAULT_ID,
        )
        co2 = EntityCooccurrence(
            entity_id_1=min(e2.id, e3.id),
            entity_id_2=max(e2.id, e3.id),
            cooccurrence_count=5,
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add_all([co1, co2])
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get(f'/api/v1/entities/{e2.id}/cooccurrences')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 2

        # Verify enriched fields are present
        for row in data:
            assert 'entity_1_name' in row
            assert 'entity_1_type' in row
            assert 'entity_2_name' in row
            assert 'entity_2_type' in row

        # Verify actual values
        names = {row['entity_1_name'] for row in data} | {row['entity_2_name'] for row in data}
        assert 'PostgreSQL' in names
        assert 'Memex' in names
        assert 'FastAPI' in names

        types = {row['entity_1_type'] for row in data} | {row['entity_2_type'] for row in data}
        assert 'Technology' in types
        assert 'Software' in types
        assert 'Framework' in types
