import base64
import time

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_batch_ingestion(client: TestClient):
    """
    Test full batch ingestion flow:
    1. POST /api/v1/ingestions/batch
    2. Polling GET /api/v1/ingestions/{job_id}
    3. Verify completion and document creation
    """
    # 1. Prepare Batch Request
    note1 = {
        'name': 'E2E Note 1',
        'description': 'Batch Item 1',
        'content': base64.b64encode(b'Content for note 1').decode('utf-8'),
        'files': {'img1.png': base64.b64encode(b'fake-image-1').decode('utf-8')},
        'tags': ['e2e', 'batch'],
    }
    note2 = {
        'name': 'E2E Note 2',
        'description': 'Batch Item 2',
        'content': base64.b64encode(b'Content for note 2').decode('utf-8'),
        'tags': ['e2e'],
    }

    payload = {'notes': [note1, note2], 'batch_size': 1}

    # 2. Submit Job
    response = client.post('/api/v1/ingestions/batch', json=payload)
    assert response.status_code == 202
    job_data = response.json()
    job_id = job_data['job_id']
    assert job_data['status'] == 'pending'

    # 3. Poll for Completion
    max_retries = 20
    poll_interval = 0.5
    completed_job = None

    for _ in range(max_retries):
        status_resp = client.get(f'/api/v1/ingestions/{job_id}')
        assert status_resp.status_code == 200
        status_data = status_resp.json()

        if status_data['status'] in ['completed', 'failed']:
            completed_job = status_data
            break

        time.sleep(poll_interval)

    assert completed_job is not None, 'Job timed out'
    assert completed_job['status'] == 'completed', f'Job failed: {completed_job.get("result")}'

    result = completed_job['result']
    assert result['processed_count'] == 2
    assert result['skipped_count'] == 0
    assert result['failed_count'] == 0
    assert len(result['note_ids']) == 2

    # 4. Verify Idempotency (Submit same batch again)
    response_dup = client.post('/api/v1/ingestions/batch', json=payload)
    assert response_dup.status_code == 202
    job_id_dup = response_dup.json()['job_id']

    # Poll again
    completed_job_dup = None
    for _ in range(max_retries):
        status_resp = client.get(f'/api/v1/ingestions/{job_id_dup}')
        status_data = status_resp.json()
        if status_data['status'] == 'completed':
            completed_job_dup = status_data
            break
        time.sleep(poll_interval)

    assert completed_job_dup is not None
    assert completed_job_dup['result']['processed_count'] == 0
    assert completed_job_dup['result']['skipped_count'] == 2


@pytest.mark.integration
def test_e2e_batch_job_not_found(client: TestClient):
    """Verify 404 for non-existent job ID."""
    fake_id = '00000000-0000-0000-0000-000000000000'
    response = client.get(f'/api/v1/ingestions/{fake_id}')
    assert response.status_code == 404
