import base64
import uuid
import asyncio
import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_batch_ingest_progress_polling(client: TestClient):
    """
    Test that batch ingestion progress is updated and can be polled.
    We ingest multiple notes with a small batch size to ensure multiple progress updates.
    """
    # 1. Prepare 5 notes
    notes = []
    for i in range(5):
        unique_id = str(uuid.uuid4())[:8]
        notes.append(
            {
                'name': f'Progress Note {i} {unique_id}',
                'description': f'Testing progress {i}',
                'content': base64.b64encode(f'Content for note {i}'.encode()).decode(),
                'tags': ['test', 'progress'],
            }
        )

    # 2. Trigger batch ingestion with batch_size=2
    # This should result in 3 chunks (2, 2, 1)
    response = client.post('/api/v1/ingest/batch', json={'notes': notes, 'batch_size': 2})
    assert response.status_code == 202
    job_id = response.json()['job_id']
    assert job_id

    # 3. Poll for progress
    # We expect to see progress strings like "Processed X/5 notes"
    max_retries = 20
    found_progress = False
    completed = False

    for _ in range(max_retries):
        status_resp = client.get(f'/api/v1/ingest/batch/{job_id}')
        assert status_resp.status_code == 200
        data = status_resp.json()

        progress = data.get('progress')
        status = data.get('status')

        if progress and 'processed' in progress.lower():
            found_progress = True

        if status == 'completed':
            completed = True
            result = data.get('result', {})
            assert result.get('processed_count') == 5
            assert 'Completed' in data['progress']
            break

        await asyncio.sleep(0.5)

    assert found_progress, (
        f"Progress field was never populated with 'Processed X/5' string. Final data: {data}"
    )
    assert completed, 'Job did not reach completed status'
