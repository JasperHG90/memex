"""Integration test: user_notes must survive the background ingestion path.

Regression test for the bug where ``ingest_batch_internal`` bypassed
``NoteInput.__init__`` and silently dropped ``user_notes`` from the DTO.
"""

import base64
import time
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
@pytest.mark.llm
def test_background_ingestion_preserves_user_notes(client: TestClient):
    """user_notes sent via background ingestion must appear in original_text."""
    user_notes_text = f'My personal commentary {uuid.uuid4()}'
    note_content = f'---\nsource_url: https://example.com\n---\nArticle body {uuid.uuid4()}'

    note = {
        'name': 'User Notes Test',
        'description': 'Test that user_notes survives background ingestion',
        'content': base64.b64encode(note_content.encode('utf-8')).decode('utf-8'),
        'tags': ['test', 'user-notes'],
        'user_notes': user_notes_text,
    }

    # Submit with background=true
    response = client.post('/api/v1/ingestions?background=true', json=note)
    assert response.status_code == 202
    job_id = response.json()['job_id']

    # Poll until completed
    status_data: dict = {}
    for _ in range(30):
        status_resp = client.get(f'/api/v1/ingestions/{job_id}')
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        if status_data['status'] in ('completed', 'failed'):
            break
        time.sleep(1)

    assert status_data['status'] == 'completed', f'Job did not complete: {status_data}'
    result = status_data['result']
    assert result['processed_count'] == 1
    note_id = result['note_ids'][0]

    # Retrieve the note and verify user_notes is in original_text
    note_resp = client.get(f'/api/v1/notes/{note_id}')
    assert note_resp.status_code == 200
    note_data = note_resp.json()

    original_text = note_data['original_text']
    assert '## User Notes' in original_text, (
        f'User Notes section missing from original_text: {original_text[:200]}'
    )
    assert user_notes_text in original_text, (
        f'User notes text not found in original_text: {original_text[:200]}'
    )

    # Verify positioning: user notes after frontmatter, before body
    fm_end = original_text.index('---', 3) + 3
    notes_pos = original_text.index('## User Notes')
    body_pos = original_text.index('Article body')
    assert fm_end < notes_pos < body_pos


@pytest.mark.integration
@pytest.mark.llm
def test_foreground_ingestion_preserves_user_notes(client: TestClient):
    """Sanity check: user_notes works in foreground mode too."""
    user_notes_text = f'Foreground commentary {uuid.uuid4()}'
    note_content = f'---\ntitle: FG Test\n---\nForeground body {uuid.uuid4()}'

    note = {
        'name': 'Foreground User Notes Test',
        'description': 'Test foreground user_notes',
        'content': base64.b64encode(note_content.encode('utf-8')).decode('utf-8'),
        'tags': ['test', 'user-notes'],
        'user_notes': user_notes_text,
    }

    response = client.post('/api/v1/ingestions', json=note)
    assert response.status_code == 200
    note_id = response.json()['note_id']

    note_resp = client.get(f'/api/v1/notes/{note_id}')
    assert note_resp.status_code == 200
    original_text = note_resp.json()['original_text']

    assert '## User Notes' in original_text
    assert user_notes_text in original_text


@pytest.mark.integration
@pytest.mark.llm
def test_background_ingestion_without_user_notes(client: TestClient):
    """When user_notes is omitted, no ## User Notes section should appear."""
    note_content = f'---\ntitle: No Notes\n---\nPlain body {uuid.uuid4()}'

    note = {
        'name': 'No User Notes Test',
        'description': 'Test without user_notes',
        'content': base64.b64encode(note_content.encode('utf-8')).decode('utf-8'),
        'tags': ['test'],
        # user_notes deliberately omitted
    }

    response = client.post('/api/v1/ingestions?background=true', json=note)
    assert response.status_code == 202
    job_id = response.json()['job_id']

    status_data: dict = {}
    for _ in range(30):
        status_resp = client.get(f'/api/v1/ingestions/{job_id}')
        status_data = status_resp.json()
        if status_data['status'] in ('completed', 'failed'):
            break
        time.sleep(1)

    assert status_data['status'] == 'completed'
    note_id = status_data['result']['note_ids'][0]

    note_resp = client.get(f'/api/v1/notes/{note_id}')
    assert note_resp.status_code == 200
    original_text = note_resp.json()['original_text']

    assert '## User Notes' not in original_text
