import pytest
import json
from uuid import uuid4, UUID
import base64
from fastapi.testclient import TestClient


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.integration
def test_e2e_doc_search_chunks_sync(client: TestClient):
    """
    Test that doc search correctly retrieves raw document chunks.
    Verifies:
    1. Chunks are embedded during ingestion.
    2. Document search uses hybrid retrieval on chunks.
    3. Results contain relevant snippets.
    """
    # 1. Ingest a document with specific text
    unique_id = str(uuid4())
    unique_text = (
        f'The secret code for this test is {unique_id} and it involves quantum flabbergasted ducks.'
    )

    note_payload = {
        'name': 'Chunk Test Doc',
        'description': 'A document for testing chunk search',
        'content': base64.b64encode(unique_text.encode()).decode(),
        'tags': ['test'],
    }

    response = client.post('/api/v1/ingestions', json=note_payload)
    assert response.status_code == 200
    ingest_res = response.json()
    document_id = ingest_res['document_id']

    # 2. Search for unique text in doc search
    search_payload = {'query': 'quantum flabbergasted ducks', 'limit': 5}

    search_resp = client.post('/api/v1/notes/search', json=search_payload)
    assert search_resp.status_code == 200
    results = parse_ndjson(search_resp.text)

    assert len(results) > 0

    # 3. Verify our document is in results with the correct snippet
    found = False
    for res in results:
        # Compare UUIDs properly
        if str(UUID(str(res['document_id']))) == str(UUID(str(document_id))):
            found = True
            assert any('quantum flabbergasted ducks' in s['text'].lower() for s in res['snippets'])
            break

    assert found, (
        f"Document {document_id} not found in search results for 'quantum flabbergasted ducks'"
    )
