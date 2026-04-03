"""E2E tests: ingest→search→lineage pipeline and note deletion cleanup."""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata


def parse_ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.integration
@pytest.mark.llm
def test_search_results_resolvable_via_lineage(client: TestClient):
    """Verify that every ID returned by memory search can be resolved by the
    lineage endpoint (both memory_unit and note entity types)."""

    # 1. Create a vault
    resp = client.post('/api/v1/vaults', json={'name': 'Lineage Test Vault'})
    assert resp.status_code == 200
    vault_id = UUID(resp.json()['id'])

    # 2. Prepare mock extraction data
    now = datetime.now(timezone.utc)
    mock_facts = [
        ExtractedFact(
            fact_text='Melle Boersma is a Dutch cyclist.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
        ),
        ExtractedFact(
            fact_text='Melle Boersma won the 2025 Amstel Gold Race.',
            fact_type='event',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
        ),
    ]
    mock_chunks = [
        ChunkMetadata(
            chunk_text=('Melle Boersma is a Dutch cyclist who won the 2025 Amstel Gold Race.'),
            fact_count=2,
            chunk_index=0,
            content_index=0,
        )
    ]
    mock_embeddings = [[0.1] * 384] * len(mock_facts)

    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    with patch(extract_path) as mock_extract, patch(embed_path) as mock_embed:
        mock_extract.return_value = (mock_facts, mock_chunks)
        mock_embed.return_value = mock_embeddings

        # 3. Ingest note
        content = b'Melle Boersma is a Dutch cyclist who won the 2025 Amstel Gold Race.'
        b64_content = base64.b64encode(content).decode('utf-8')

        with patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ):
            ingest_resp = client.post(
                '/api/v1/ingestions',
                json={
                    'name': 'Melle Boersma Bio',
                    'description': 'Facts about Melle Boersma',
                    'content': b64_content,
                    'files': {},
                    'tags': ['cycling'],
                },
            )
            assert ingest_resp.status_code == 200, f'Ingest failed: {ingest_resp.text}'
            assert ingest_resp.json()['status'] == 'success'
            # note_id comes back as hex without dashes; normalize to dashed UUID
            note_id = str(UUID(ingest_resp.json()['note_id']))

    # 4. Search for the ingested content
    app = client.app
    real_embedder = app.state.api.embedder

    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.1] * 384]
    app.state.api.embedder = mock_embedder

    try:
        search_resp = client.post(
            '/api/v1/memories/search',
            json={
                'query': 'Melle Boersma',
                'limit': 10,
                'vault_ids': ['Lineage Test Vault'],
            },
        )
        assert search_resp.status_code == 200, f'Search failed: {search_resp.text}'
        results = parse_ndjson(search_resp.text)
        assert len(results) > 0, 'Search returned no results'
    finally:
        app.state.api.embedder = real_embedder

    # 5. Verify lineage for each memory unit returned by search
    for result in results:
        mu_id = result['id']
        resp = client.get(f'/api/v1/lineage/memory_unit/{mu_id}')
        assert resp.status_code == 200, f'Lineage 404 for memory_unit {mu_id}: {resp.text}'
        lineage = resp.json()
        assert lineage['entity_type'] == 'memory_unit'
        assert lineage['entity']['id'] == mu_id

    # 6. Verify lineage for the source note
    result_note_ids = {r['note_id'] for r in results if r.get('note_id')}
    assert note_id in result_note_ids, (
        f'Ingested note {note_id} not found in search result note_ids: {result_note_ids}'
    )
    for nid in result_note_ids:
        resp = client.get(f'/api/v1/lineage/note/{nid}')
        assert resp.status_code == 200, f'Lineage 404 for note {nid}: {resp.text}'
        lineage = resp.json()
        assert lineage['entity_type'] == 'note'
        assert lineage['entity']['id'] == nid

    # 7. Verify downstream lineage from note shows memory units
    resp = client.get(
        f'/api/v1/lineage/note/{note_id}',
        params={'direction': 'downstream', 'depth': 2},
    )
    assert resp.status_code == 200
    lineage = resp.json()
    assert len(lineage['derived_from']) > 0, 'Note downstream lineage should contain memory units'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_note_prunes_mental_model_observations(postgres_container):
    """Deleting a note via the API must prune mental model observations that
    cited the deleted note's memory units. This is the root cause of the
    lineage 404 bug: stale observations survive deletion and get returned
    by the mental_model retrieval strategy as virtual units."""
    import asyncio
    from httpx import AsyncClient, ASGITransport
    from sqlmodel import col, select
    from memex_core.memory.sql_models import (
        Entity,
        MentalModel,
        MemoryUnit,
        Note,
        UnitEntity,
    )
    from memex_core.server import app as server_app, lifespan
    from memex_common.config import GLOBAL_VAULT_ID
    from memex_common.types import FactTypes

    async with lifespan(server_app):
        api = server_app.state.api

        vault_id = GLOBAL_VAULT_ID
        entity_id = uuid4()
        note_a_id = uuid4()
        note_b_id = uuid4()
        unit_a_id = uuid4()
        unit_b_id = uuid4()
        obs_shared_id = uuid4()
        obs_only_a_id = uuid4()
        now = datetime.now(timezone.utc)

        # Setup: create test data
        async with api.metastore.session() as session:
            session.add(Entity(id=entity_id, canonical_name='TestEnt', vault_id=vault_id))
            session.add(Note(id=note_a_id, vault_id=vault_id, original_text='A'))
            session.add(Note(id=note_b_id, vault_id=vault_id, original_text='B'))
            await session.flush()
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
            session.add(
                MentalModel(
                    entity_id=entity_id,
                    vault_id=vault_id,
                    name='TestEnt',
                    observations=[
                        {
                            'id': str(obs_shared_id),
                            'title': 'Shared',
                            'content': 'Both',
                            'trend': 'new',
                            'evidence': [
                                {'memory_id': str(unit_a_id), 'quote': 'A', 'relevance': 1.0},
                                {'memory_id': str(unit_b_id), 'quote': 'B', 'relevance': 1.0},
                            ],
                        },
                        {
                            'id': str(obs_only_a_id),
                            'title': 'OnlyA',
                            'content': 'A only',
                            'trend': 'new',
                            'evidence': [
                                {'memory_id': str(unit_a_id), 'quote': 'A', 'relevance': 1.0},
                            ],
                        },
                    ],
                    version=1,
                )
            )
            await session.commit()

        # Act: delete note A via HTTP
        async with AsyncClient(
            transport=ASGITransport(app=server_app), base_url='http://test'
        ) as http:
            resp = await http.delete(f'/api/v1/notes/{note_a_id}')
            assert resp.status_code == 200, f'Delete failed: {resp.text}'

        # Allow background tasks to settle
        await asyncio.sleep(0.5)

        # Assert: mental model observations are pruned
        async with api.metastore.session() as session:
            mm = (
                await session.exec(
                    select(MentalModel).where(col(MentalModel.entity_id) == entity_id)
                )
            ).first()

        assert mm is not None, 'Mental model should survive (note B still exists)'

        obs_ids = {o['id'] for o in mm.observations}
        assert str(obs_only_a_id) not in obs_ids, (
            'Observation with evidence only from deleted note should be removed'
        )
        assert str(obs_shared_id) in obs_ids, 'Observation with mixed evidence should survive'
        shared = next(o for o in mm.observations if o['id'] == str(obs_shared_id))
        assert len(shared['evidence']) == 1
        assert shared['evidence'][0]['memory_id'] == str(unit_b_id)
