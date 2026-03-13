import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID
from memex_core.processing.batch import JobManager
from memex_core.memory.sql_models import BatchJob, BatchJobStatus


@pytest.fixture
def mock_api(mock_metastore):
    api = MagicMock()
    # ingest_batch_internal returns an async generator, so we use MagicMock
    # instead of AsyncMock (which would return a coroutine).
    api.ingest_batch_internal = MagicMock()
    api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    api.metastore = mock_metastore
    api.config.server.default_active_vault = 'global'
    return api


@pytest.fixture
def manager(mock_api):
    return JobManager(mock_api)


@pytest.mark.asyncio
async def test_create_job(manager, mock_api, mock_session):
    """Test job creation and initial persistence."""
    notes = [MagicMock()]
    vault_id = uuid4()

    # Patch _run_job to prevent background execution during this test
    with patch.object(manager, '_run_job', new_callable=AsyncMock) as _:
        job_id = await manager.create_job(notes, vault_id)

    assert isinstance(job_id, UUID)
    mock_session.add.assert_called()
    mock_session.commit.assert_called()
    # Verify the background task was scheduled (called)
    # Since create_task calls it, we check if the coroutine was created?
    # Actually, patch.object replaces the method. create_job calls self._run_job(...)
    # Since it's an async method, it returns a coroutine. create_task schedules it.
    # The mock needs to be awaited? No, create_task takes a coroutine.
    # If _run_job is AsyncMock, calling it returns a coroutine (which is mock_run.return_value if configured, or a new coroutine).
    # We just want to ensure it doesn't execute real logic.


@pytest.mark.asyncio
async def test_run_job_success(manager, mock_api, mock_session):
    """Test background job execution success path."""
    job_id = uuid4()
    vault_id = uuid4()
    notes = [MagicMock()]

    # Mock job retrieval
    job = BatchJob(
        id=job_id, vault_id=vault_id, status=BatchJobStatus.PENDING, notes_count=len(notes)
    )
    mock_session.get.return_value = job

    # Mock API ingestion to return an async generator
    async def mock_ingest_gen(*args, **kwargs):
        yield {
            'processed_count': 1,
            'skipped_count': 0,
            'failed_count': 0,
            'note_ids': [str(uuid4())],
            'errors': [],
        }

    mock_api.ingest_batch_internal.side_effect = mock_ingest_gen

    await manager._run_job(job_id, notes, vault_id)

    assert job.status == BatchJobStatus.COMPLETED
    assert job.processed_count == 1
    assert job.completed_at is not None
    mock_session.commit.assert_called()


@pytest.mark.asyncio
async def test_run_job_failure(manager, mock_api, mock_session):
    """Test background job execution failure path."""
    job_id = uuid4()
    vault_id = uuid4()
    notes = [MagicMock()]

    job = BatchJob(
        id=job_id, vault_id=vault_id, status=BatchJobStatus.PENDING, notes_count=len(notes)
    )
    mock_session.get.return_value = job

    # Mock API ingestion to raise exception when iterated
    async def mock_ingest_gen_fail(*args, **kwargs):
        raise Exception('Fatal Error')
        yield {}

    mock_api.ingest_batch_internal.side_effect = mock_ingest_gen_fail

    await manager._run_job(job_id, notes, vault_id)

    assert job.status == BatchJobStatus.FAILED
    assert 'Fatal Error' in job.error_info
    mock_session.commit.assert_called()
