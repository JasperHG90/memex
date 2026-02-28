import asyncio
import base64
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm
async def test_ingest_background_returns_job_id(client: TestClient):
    """Background ingestion returns a job_id and completes successfully."""
    note = {
        'name': 'Background Test',
        'description': 'Test background ingestion',
        'content': base64.b64encode(f'unique content {uuid.uuid4()}'.encode()).decode('utf-8'),
        'tags': ['test', 'background'],
    }

    # Submit with background=true — expect 202 with a job_id
    response = client.post('/api/v1/ingestions?background=true', json=note)
    assert response.status_code == 202
    data = response.json()
    assert 'job_id' in data
    assert data['status'] == 'pending'
    job_id = data['job_id']

    # Poll until completed (max 30s)
    status = None
    status_data: dict = {}
    for _ in range(30):
        status_resp = client.get(f'/api/v1/ingestions/{job_id}')
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        status = status_data['status']
        if status in ('completed', 'failed'):
            break
        await asyncio.sleep(1)

    assert status == 'completed', f'Job did not complete: {status_data}'
    result = status_data['result']
    assert result['processed_count'] == 1
    assert result['failed_count'] == 0
    assert len(result['note_ids']) == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm
async def test_ingest_foreground_still_works(client: TestClient):
    """Without background flag, ingestion still returns 200 with note_id."""
    note = {
        'name': 'Foreground Test',
        'description': 'Test foreground ingestion',
        'content': base64.b64encode(f'foreground content {uuid.uuid4()}'.encode()).decode('utf-8'),
        'tags': ['test', 'foreground'],
    }

    response = client.post('/api/v1/ingestions', json=note)
    assert response.status_code == 200
    data = response.json()
    assert 'note_id' in data
    assert data['note_id'] is not None
