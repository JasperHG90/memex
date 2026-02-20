from uuid import uuid4
from memex_common.schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    BatchJobStatus,
    NoteDTO,
)


def test_batch_ingest_request_schema():
    """Test the BatchIngestRequest schema validation."""
    note = NoteDTO(
        name='Test Note',
        description='A test note',
        content='SGVsbG8gd29ybGQ=',  # "Hello world"
        tags=['test'],
    )

    # Valid request
    request = BatchIngestRequest(notes=[note], batch_size=10, vault_id=uuid4())
    assert len(request.notes) == 1
    assert request.batch_size == 10
    assert request.vault_id is not None

    # Default values
    request_default = BatchIngestRequest(notes=[note])
    assert request_default.batch_size == 32
    assert request_default.vault_id is None


def test_batch_ingest_response_schema():
    """Test the BatchIngestResponse schema validation."""
    doc_id = str(uuid4())
    response = BatchIngestResponse(
        processed_count=5,
        skipped_count=2,
        failed_count=1,
        document_ids=[doc_id],
        errors=[{'index': 7, 'message': 'Failed to process'}],
    )
    assert response.processed_count == 5
    assert response.skipped_count == 2
    assert response.failed_count == 1
    assert response.document_ids == [doc_id]
    assert response.errors[0]['index'] == 7


def test_batch_job_status_schema():
    """Test the BatchJobStatus schema validation."""
    job_id = uuid4()

    # Pending status
    status_pending = BatchJobStatus(job_id=job_id, status='pending')
    assert status_pending.status == 'pending'
    assert status_pending.result is None

    # Completed status with result
    result = BatchIngestResponse(
        processed_count=1, skipped_count=0, failed_count=0, document_ids=[str(uuid4())]
    )
    status_completed = BatchJobStatus(job_id=job_id, status='completed', result=result)
    assert status_completed.status == 'completed'
    assert status_completed.result.processed_count == 1
