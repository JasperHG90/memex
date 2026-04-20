"""Integration tests for entity type filtering across the stack."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from memex_core.memory.sql_models import Entity
from memex_core.server import app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_by_type(api, metastore, init_global_vault):
    """Filter by a specific entity type returns only matching entities."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        session.add(Entity(canonical_name='Acme Corp', entity_type='Organization', mention_count=2))
        await session.commit()

    persons = await api.get_top_entities(limit=10, entity_type='Person')
    assert len(persons) == 1
    assert persons[0].entity.canonical_name == 'Alice'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_none_returns_all(api, metastore, init_global_vault):
    """entity_type=None returns all entities (no filtering)."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        await session.commit()

    entities = await api.get_top_entities(limit=10, entity_type=None)
    assert len(entities) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_nonexistent_type_returns_empty(api, metastore, init_global_vault):
    """An entity type not present in the DB returns empty results."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        await session.commit()

    entities = await api.get_top_entities(limit=10, entity_type='Location')
    assert len(entities) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_case_sensitive(api, metastore, init_global_vault):
    """Entity type filter is case-sensitive — lowercase 'person' won't match 'Person'."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        await session.commit()

    entities = await api.get_top_entities(limit=10, entity_type='person')
    assert len(entities) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_entities_with_type_filter(api, metastore, init_global_vault):
    """search_entities combines query and entity_type filter."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=10))
        session.add(Entity(canonical_name='Python Snake', entity_type='Concept', mention_count=5))
        await session.commit()

    results = await api.search_entities(query='Python', limit=10, entity_type='Technology')
    assert len(results) == 1
    assert results[0].entity.canonical_name == 'Python'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_top_entities_ranking_within_type(api, metastore, init_global_vault):
    """get_top_entities ranks by mention_count within the filtered type."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='FastAPI', entity_type='Technology', mention_count=20))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=50))
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=100))
        await session.commit()

    results = await api.get_top_entities(limit=10, entity_type='Technology')
    assert len(results) == 2
    assert results[0].entity.canonical_name == 'Python'
    assert results[1].entity.canonical_name == 'FastAPI'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_type_filter_rest_endpoint_validation(api, metastore, init_global_vault):
    """REST endpoint returns 422 for invalid entity_type values."""
    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        # Valid type
        response = await ac.get('/api/v1/entities?entity_type=Person&sort=-mentions')
        assert response.status_code == 200

        # Invalid type — should get 422 from FastAPI enum validation
        response = await ac.get('/api/v1/entities?entity_type=InvalidType&sort=-mentions')
        assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_type_filter_rest_endpoint_streaming(api, metastore, init_global_vault):
    """REST endpoint filters and streams entities by type."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/entities?entity_type=Person&sort=-mentions')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['name'] == 'Alice'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_null_type_excluded(api, metastore, init_global_vault):
    """Entities with NULL entity_type are excluded when filtering by a specific type."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Unknown', entity_type=None, mention_count=10))
        await session.commit()

    results = await api.get_top_entities(limit=10, entity_type='Person')
    assert len(results) == 1
    assert results[0].entity.canonical_name == 'Alice'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_entities_null_type_included_when_no_filter(api, metastore, init_global_vault):
    """Entities with NULL entity_type are included when entity_type=None (no filter)."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Unknown', entity_type=None, mention_count=10))
        await session.commit()

    results = await api.get_top_entities(limit=10, entity_type=None)
    assert len(results) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_entities_ranked_with_type_filter(api, metastore, init_global_vault):
    """list_entities_ranked async generator respects entity_type filter."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        await session.commit()

    results = []
    async for entity in api.list_entities_ranked(limit=10, entity_type='Technology'):
        results.append(entity)
    assert len(results) == 1
    assert results[0].entity.canonical_name == 'Python'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_entities_ranked_no_filter_returns_all(api, metastore, init_global_vault):
    """list_entities_ranked without entity_type returns all entities."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        await session.commit()

    results = []
    async for entity in api.list_entities_ranked(limit=10, entity_type=None):
        results.append(entity)
    assert len(results) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_default_path_uses_ranked_with_type_filter(api, metastore, init_global_vault):
    """REST endpoint without sort or q uses list_entities_ranked, which filters by type."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Alice', entity_type='Person', mention_count=5))
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=3))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/entities?entity_type=Person')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['name'] == 'Alice'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_search_with_type_filter(api, metastore, init_global_vault):
    """REST endpoint with q and entity_type filters search results."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(Entity(canonical_name='Python', entity_type='Technology', mention_count=10))
        session.add(Entity(canonical_name='Python Snake', entity_type='Concept', mention_count=5))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get(
            '/api/v1/entities?query=Python&entity_type=Technology&sort=-mentions'
        )
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['name'] == 'Python'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_database_returns_empty(api, metastore, init_global_vault):
    """Filtering on an empty database returns no results."""
    await api.initialize()

    results = await api.get_top_entities(limit=10, entity_type='Person')
    assert len(results) == 0

    search_results = await api.search_entities(query='anything', limit=10, entity_type='Person')
    assert len(search_results) == 0

    ranked = []
    async for entity in api.list_entities_ranked(limit=10, entity_type='Person'):
        ranked.append(entity)
    assert len(ranked) == 0
