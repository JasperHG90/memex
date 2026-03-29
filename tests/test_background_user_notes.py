"""Integration test: user_notes must survive the background ingestion path.

Regression test for the bug where ``ingest_batch_internal`` bypassed
``NoteInput.__init__`` and silently dropped ``user_notes`` from the DTO.
"""

import base64
import re
import time
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

_FM_RE = re.compile(r'\A---[ \t]*\n(.*?\n)---[ \t]*\n', re.DOTALL)


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
    m = _FM_RE.match(original_text)
    assert m, f'No frontmatter found in original_text: {original_text[:200]}'
    fm = yaml.safe_load(m.group(1))
    # YAML block scalar (|) appends a trailing newline; strip for comparison
    assert fm.get('user_notes', '').rstrip('\n') == user_notes_text, (
        f'user_notes field missing or wrong in frontmatter: {fm}'
    )
    assert 'Article body' in original_text


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

    m = _FM_RE.match(original_text)
    assert m, f'No frontmatter found in original_text: {original_text[:200]}'
    fm = yaml.safe_load(m.group(1))
    assert fm.get('user_notes', '').rstrip('\n') == user_notes_text


@pytest.mark.integration
@pytest.mark.llm
def test_background_ingestion_without_user_notes(client: TestClient):
    """When user_notes is omitted, no user_notes field should appear in frontmatter."""
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

    m = _FM_RE.match(original_text)
    if m:
        fm = yaml.safe_load(m.group(1)) or {}
        assert 'user_notes' not in fm
    assert 'user_notes' not in original_text
