import base64
import json
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from uuid import UUID

import pytest

from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata
from memex_core.memory.sql_models import TokenUsage


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.integration
@pytest.mark.llm
def test_workflow_ingest_retrieve(client: TestClient):
    """
    Test a full workflow:
    1. Create a Vault
    2. Ingest a Note (Mocking LLM extraction and Embedding)
    3. Retrieve the Note (Verify DB storage and Search)
    """

    # 1. Create Vault
    vault_name = 'Workflow Vault'
    resp = client.post('/api/v1/vaults', json={'name': vault_name})
    assert resp.status_code == 200
    vault_id_str = resp.json()['id']
    vault_id = UUID(vault_id_str)

    # Prepare Mock Data
    now = datetime.now(timezone.utc)
    mock_facts = [
        ExtractedFact(
            fact_text='Alice is a software engineer.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
            confidence=0.9,
        ),
        ExtractedFact(
            fact_text='Alice lives in Wonderland.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
            confidence=0.8,
        ),
    ]
    mock_chunks = [
        ChunkMetadata(
            chunk_text='Alice is a software engineer who lives in Wonderland.',
            fact_count=2,
            chunk_index=0,
            content_index=0,
        )
    ]
    mock_usage = TokenUsage(total_tokens=100)

    # 384 dimensions
    mock_embeddings = [[0.1] * 384] * len(mock_facts)

    # Patch Paths
    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    with patch(extract_path) as mock_extract, patch(embed_path) as mock_embed:
        # Configure Mocks
        mock_extract.return_value = (mock_facts, mock_chunks, mock_usage)
        mock_embed.return_value = mock_embeddings

        # 2. Ingest Note
        note_content = b'Alice is a software engineer who lives in Wonderland.'
        b64_content = base64.b64encode(note_content).decode('utf-8')

        with patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ):
            payload = {
                'name': 'Alice Bio',
                'description': 'Bio of Alice',
                'content': b64_content,
                'files': {},
                'tags': ['person', 'bio'],
            }

            ingest_resp = client.post('/api/v1/ingestions', json=payload)
            assert ingest_resp.status_code == 200, f'Ingest failed: {ingest_resp.text}'
            ingest_data = ingest_resp.json()
            assert ingest_data['status'] == 'success'

        # 3. Retrieve
        app = client.app
        real_embedder = app.state.api.embedder

        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = [[0.1] * 384]
        app.state.api.embedder = mock_embedder

        try:
            # We explicitly scope search to the vault we created, using its NAME
            retrieve_payload = {
                'query': 'Alice',
                'limit': 5,
                'vault_ids': [vault_name],
                'skip_opinion_formation': True,
            }

            retrieve_resp = client.post('/api/v1/memories/search', json=retrieve_payload)
            assert retrieve_resp.status_code == 200, f'Retrieve failed: {retrieve_resp.text}'

            results = parse_ndjson(retrieve_resp.text)
            assert len(results) > 0
            assert any('Alice' in r['text'] for r in results)

        finally:
            app.state.api.embedder = real_embedder
