import pytest
import json
import base64
from uuid import uuid4
from fastapi.testclient import TestClient


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_incremental_idempotency(client: TestClient):
    """
    Verifies that:
    1. Re-ingesting the exact same content results in 0 new memory units (idempotency).
    2. Editing one part of a document only re-extracts that block.
    3. Unchanged blocks' memory units are preserved (not marked stale).
    """

    # 1. Setup multi-block content
    # block_size = 4000
    # Use realistic lines to allow Line-Based CDC to find boundaries
    block1 = 'SECTION ONE: The quick brown fox jumps over the lazy dog.\n' * 40  # ~2400 chars
    block2 = "SECTION TWO: A wizard's quickly-moving jab excited a phantom.\n" * 40  # ~2400 chars

    content_v1 = block1 + '\n\n' + block2

    # We use note_key for stable document identification across calls
    note_key = f'incremental-test-{uuid4()}'

    payload_v1 = {
        'name': 'Incremental Test Doc',
        'description': 'Initial version',
        'content': base64.b64encode(content_v1.encode()).decode(),
        'note_key': note_key,
    }

    # Ingest V1
    print('\n--- Ingesting V1 ---')
    resp1 = client.post('/api/v1/ingestions', json=payload_v1)
    assert resp1.status_code == 200, f'V1 Ingest failed: {resp1.text}'
    doc_id = resp1.json()['note_id']
    unit_ids_v1 = resp1.json()['unit_ids']
    print(f'V1 Document ID: {doc_id}')
    print(f'V1 Unit IDs count: {len(unit_ids_v1)}')
    assert len(unit_ids_v1) > 0

    # 2. Idempotency Check: Ingest V1 again
    print('\n--- Re-ingesting V1 (Idempotency) ---')
    resp2 = client.post('/api/v1/ingestions', json=payload_v1)
    assert resp2.status_code == 200, f'V1 Re-ingest failed: {resp2.text}'
    unit_ids_v2 = resp2.json()['unit_ids']
    print(f'V1 Re-ingest Unit IDs count: {len(unit_ids_v2)}')
    assert len(unit_ids_v2) == 0, 'Idempotency failed: New units created for identical content'

    # 3. Incremental Update: Change only SECTION TWO
    print('\n--- Ingesting V2 (Incremental Update) ---')
    block2_mod = (
        'SECTION TWO: The wizard was replaced by a giant robotic hamster.\n' * 40
    )  # ~2400 chars
    content_v2 = block1 + '\n\n' + block2_mod

    payload_v2 = {
        'name': 'Incremental Test Doc',
        'description': 'Updated version',
        'content': base64.b64encode(content_v2.encode()).decode(),
        'note_key': note_key,
    }

    resp3 = client.post('/api/v1/ingestions', json=payload_v2)
    assert resp3.status_code == 200, f'V2 Ingest failed: {resp3.text}'
    unit_ids_v3 = resp3.json()['unit_ids']
    print(f'V2 Unit IDs count (added): {len(unit_ids_v3)}')
    assert len(unit_ids_v3) > 0

    # 4. Verify stale propagation
    print('\n--- Verifying Stale Propagation ---')
    # Search for the old content (wizard)
    search_wizard = client.post(
        '/api/v1/memories/search',
        json={
            'query': 'quickly-moving jab excited a phantom',
            'limit': 10,
            'skip_opinion_formation': True,
        },
    )
    results_wizard = parse_ndjson(search_wizard.text)

    # Strictly check for active units containing specific stale content
    # The new content mentions "wizard" ("wizard was replaced"), so we must check for the OLD action ("jab", "phantom")
    stale_phrase = 'quickly-moving jab'

    # 4a. Verify we CAN retrieve stale units
    stale_units = [r for r in results_wizard if stale_phrase in r['text'].lower()]
    print(f"Found {len(stale_units)} units with stale phrase '{stale_phrase}'")
    assert len(stale_units) == 0, 'Stale units should NOT be retrievable by default'

    # 4b. Verify we CAN retrieve stale units if explicitly requested
    print('\\n--- Verifying Explicit Stale Retrieval ---')
    search_stale_explicit = client.post(
        '/api/v1/memories/search',
        json={
            'query': 'quickly-moving jab excited a phantom',
            'limit': 10,
            'skip_opinion_formation': True,
            'include_stale': True,
        },
    )
    # The endpoint returns NDJSON, parse it
    results_stale_explicit = parse_ndjson(search_stale_explicit.text)

    explicit_stale_units = [
        r
        for r in results_stale_explicit
        if stale_phrase in r['text'].lower() and r.get('status') == 'stale'
    ]
    print(f'Found {len(explicit_stale_units)} explicitly requested stale units')
    assert len(explicit_stale_units) > 0, (
        'Stale units SHOULD be retrievable when include_stale=True'
    )

    # Search for new content (hamster)
    search_hamster = client.post(
        '/api/v1/memories/search',
        json={
            'query': 'giant robotic hamster',
            'limit': 10,
            'skip_opinion_formation': True,
        },
    )
    results_hamster = parse_ndjson(search_hamster.text)
    hamster_active = [
        r for r in results_hamster if r.get('status') == 'active' and 'hamster' in r['text'].lower()
    ]
    print(f'Hamster active results found: {len(hamster_active)}')
    assert len(hamster_active) > 0, 'Robotic hamster (new content) not found in active memories'

    # 5. Verify SECTION ONE units are still active
    print('\n--- Verifying Retained Content Persistence ---')
    search_fox = client.post(
        '/api/v1/memories/search',
        json={
            'query': 'quick brown fox jumps over the lazy dog',
            'limit': 10,
            'skip_opinion_formation': True,
        },
    )
    results_fox = parse_ndjson(search_fox.text)
    fox_active = [
        r for r in results_fox if r.get('status') == 'active' and 'fox' in r['text'].lower()
    ]
    print(f'Fox active results found: {len(fox_active)}')
    assert len(fox_active) > 0, (
        'Fox (retained content) units were accidentally marked stale or lost'
    )
