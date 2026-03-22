import base64
import json
import pytest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from uuid import UUID

from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.integration
@pytest.mark.llm
def test_entity_type_partitioning_e2e(client: TestClient):
    """
    Verify that entities with same name but different types are treated as distinct.
    """
    # 1. Create Vault
    vault_name = 'Entity Test Vault'
    resp = client.post('/api/v1/vaults', json={'name': vault_name})
    vault_id = UUID(resp.json()['id'])

    now = datetime.now(timezone.utc)

    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    # Step 1: Ingest "Java" as ORG
    fact_org = ExtractedFact(
        fact_text='Java is a company.',
        fact_type='world',
        entities=[{'text': 'Java', 'type': 'ORG'}],
        chunk_index=0,
        content_index=0,
        mentioned_at=now,
        vault_id=vault_id,
    )

    with (
        patch(extract_path, new_callable=AsyncMock) as mock_extract,
        patch(embed_path, return_value=[[0.1] * 384]),
        patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ),
    ):
        mock_extract.return_value = (
            [fact_org],
            [ChunkMetadata(chunk_text='...', fact_count=1, content_index=0, chunk_index=0)],
        )

        resp = client.post(
            '/api/v1/ingestions',
            json={
                'name': 'Note 1',
                'description': 'Description 1',
                'content': base64.b64encode(b'Java is a company.').decode('utf-8'),
            },
        )
        assert resp.status_code == 200

    # Step 2: Ingest "Java" as TECHNOLOGY
    fact_tech = ExtractedFact(
        fact_text='Java is a programming language.',
        fact_type='world',
        entities=[{'text': 'Java', 'type': 'TECHNOLOGY'}],
        chunk_index=0,
        content_index=0,
        mentioned_at=now,
        vault_id=vault_id,
    )

    with (
        patch(extract_path, new_callable=AsyncMock) as mock_extract,
        patch(embed_path, return_value=[[-0.1] * 384]),
        patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ),
    ):
        mock_extract.return_value = (
            [fact_tech],
            [ChunkMetadata(chunk_text='...', fact_count=1, content_index=0, chunk_index=0)],
        )

        resp = client.post(
            '/api/v1/ingestions',
            json={
                'name': 'Note 2',
                'description': 'Description 2',
                'content': base64.b64encode(b'Java is a programming language.').decode('utf-8'),
            },
        )
        assert resp.status_code == 200

    # Step 3: Verify distinct entities are merged because EntityResolver is type-agnostic
    resp = client.get('/api/v1/entities?q=Java')
    assert resp.status_code == 200
    entities = parse_ndjson(resp.text)

    # Filter for exact name to avoid partial matches from other tests
    java_entities = [e for e in entities if e['name'] == 'Java']

    # Expect merged entity
    assert len(java_entities) == 1
    assert java_entities[0]['mention_count'] == 2
