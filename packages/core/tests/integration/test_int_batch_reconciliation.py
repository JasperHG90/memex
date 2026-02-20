import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from datetime import datetime, timezone

from memex_core.processing.batch import JobManager
from memex_core.memory.sql_models import BatchJob, BatchJobStatus


@pytest.mark.asyncio
async def test_reconcile_interrupted_jobs(metastore):
    """
    Test that reconcile_interrupted_jobs finds PROCESSING jobs and marks them FAILED.
    """
    # 1. Setup Mock API
    mock_api = MagicMock()
    mock_api.metastore = metastore

    manager = JobManager(mock_api)

    # 2. Seed Data: Create Vault then jobs
    vault_id = uuid4()
    stuck_id = uuid4()
    pending_id = uuid4()
    completed_id = uuid4()

    async with metastore.session() as session:
        from memex_core.memory.sql_models import Vault

        session.add(Vault(id=vault_id, name='Test Vault'))
        await session.commit()

        # Stuck Job
        session.add(
            BatchJob(
                id=stuck_id,
                vault_id=vault_id,
                status=BatchJobStatus.PROCESSING,
                started_at=datetime.now(timezone.utc),
                notes_count=10,
            )
        )
        # Pending Job (Should not be touched)
        session.add(
            BatchJob(id=pending_id, vault_id=vault_id, status=BatchJobStatus.PENDING, notes_count=5)
        )
        # Completed Job (Should not be touched)
        session.add(
            BatchJob(
                id=completed_id, vault_id=vault_id, status=BatchJobStatus.COMPLETED, notes_count=5
            )
        )
        await session.commit()

    # 3. Execute Reconciliation
    count = await manager.reconcile_interrupted_jobs()

    # 4. Verify
    assert count == 1

    async with metastore.session() as session:
        stuck_job = await session.get(BatchJob, stuck_id)
        assert stuck_job.status == BatchJobStatus.FAILED
        assert 'server restart' in stuck_job.error_info

        pending_job = await session.get(BatchJob, pending_id)
        assert pending_job.status == BatchJobStatus.PENDING

        completed_job = await session.get(BatchJob, completed_id)
        assert completed_job.status == BatchJobStatus.COMPLETED
