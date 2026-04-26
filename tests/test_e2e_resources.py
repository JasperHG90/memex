import base64

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
@pytest.mark.llm
def test_resource_retrieval_workflow(client: TestClient):
    """
    Test the full resource retrieval workflow:
    1. Ingest a note with an auxiliary file (image).
    2. Verify the document is created.
    3. Retrieve the resource via the API.
    """
    # 1. Prepare Note with an image
    note_content = b'# Note with image\n![Test Image](test.png)'
    image_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'  # Minimal PNG header

    payload = {
        'name': 'Resource Test Note',
        'description': 'Note with auxiliary files',
        'content': base64.b64encode(note_content).decode('utf-8'),
        'files': {'test.png': base64.b64encode(image_content).decode('utf-8')},
        'tags': ['test'],
        'vault_id': 'memex',
    }
    # Ensure vault exists
    client.post('/api/v1/vaults', json={'name': 'memex'})

    # Ingest
    resp = client.post('/api/v1/ingestions', json=payload)
    assert resp.status_code == 200
    doc_id = resp.json()['note_id']

    # 2. Get the document to find the vault name/path
    # The active vault name is 'memex' as per config
    resource_path = f'assets/memex/{doc_id}/test.png'

    # 3. Retrieve the resource
    # Note: the router handles {path:path} which includes slashes
    res_resp = client.get(f'/api/v1/resources/{resource_path}')

    assert res_resp.status_code == 200
    assert res_resp.content == image_content
    assert res_resp.headers['content-type'] == 'image/png'


@pytest.mark.integration
@pytest.mark.llm
def test_ingest_does_not_create_note_file(client: TestClient, tmp_path):
    """
    Verify that ingestion persists assets to disk but does NOT create a markdown file
    for the note content (it should only be in the DB).
    """
    # 1. Ingest a note
    note_content = b'# Secret Note\nThis should not be on disk.'
    payload = {
        'name': 'Ghost Note',
        'description': 'A note that lives only in DB',
        'content': base64.b64encode(note_content).decode('utf-8'),
        'files': {},
        'tags': ['ghost'],
        'vault_id': 'memex',
    }

    # Ensure vault exists
    client.post('/api/v1/vaults', json={'name': 'memex'})

    resp = client.post('/api/v1/ingestions', json=payload)
    assert resp.status_code == 200
    doc_id = resp.json()['note_id']

    # 2. Check Filesystem
    # The `tmp_env` fixture sets the current working directory to the temp dir.
    # We expect `assets/` to exist (potentially empty or not created if no assets),
    # but strictly NO `notes/` directory or `notes/...` file.

    import pathlib

    cwd = pathlib.Path.cwd()

    # Check that NO file with the note content exists in a 'notes' directory
    # We search recursively just in case
    found_note_files = list(cwd.rglob(f'*{doc_id}*.md'))
    assert len(found_note_files) == 0, f'Found unexpected note files on disk: {found_note_files}'

    # Verify DB persistence (via API)
    get_resp = client.get(f'/api/v1/notes/{doc_id}')
    assert get_resp.status_code == 200

    # Normalize UUIDs for comparison (ingest returns hex, get returns UUID string)
    import uuid

    assert uuid.UUID(get_resp.json()['id']) == uuid.UUID(doc_id)


@pytest.mark.integration
def test_resource_not_found(client: TestClient):
    """Test 404 for non-existent resource."""
    resp = client.get('/api/v1/resources/non/existent/path.txt')
    assert resp.status_code == 404


# AC-001 empty/dot-path 404 coverage lives in
# ``packages/core/tests/integration/test_server_resources.py`` so it stays
# co-located with the route module and runs without the ``llm`` marker.
