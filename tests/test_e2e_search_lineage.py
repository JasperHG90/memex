"""E2E test: ingest a note, search for it, then verify lineage returns 200."""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from uuid import UUID

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
