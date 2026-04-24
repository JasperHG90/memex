import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from memex_core.context import background_session
from memex_core.memory.sql_models import BatchJob, BatchJobStatus

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.processing.batch')


class JobManager:
    """
    Manages asynchronous batch ingestion jobs.
    Handles job persistence, background execution, and status tracking.
    """

    def __init__(self, api: 'MemexAPI'):
        """
        Initialize the JobManager.

        Args:
            api: Instance of MemexAPI.
        """
        self.api = api
        self._active_tasks: dict[UUID, asyncio.Task] = {}

    def _task_done_callback(self, job_id: UUID, task: asyncio.Task) -> None:
        """Log unhandled exceptions from background tasks and clean up references."""
        self._active_tasks.pop(job_id, None)
        if task.cancelled():
            logger.warning('Batch job %s task was cancelled.', job_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.error('Batch job %s raised unhandled exception: %s', job_id, exc, exc_info=exc)

    async def create_job(
        self,
        notes: list[Any],
        vault_id: UUID | str | None = None,
        batch_size: int = 32,
        background_tasks: Any | None = None,
    ) -> UUID:
        """
        Create a new batch job and start it in the background.

        When *background_tasks* (a Starlette ``BackgroundTasks`` instance) is
        provided, the job is scheduled through it so that the ASGI server manages
        the task lifecycle.  Otherwise an ``asyncio.Task`` is created directly.

        Args:
            notes: List of NoteDTOs to ingest.
            vault_id: Optional target vault identifier.
            batch_size: Processing chunk size.
            background_tasks: Optional Starlette ``BackgroundTasks`` instance.

        Returns:
            UUID: The created Job ID.
        """
        job_id = uuid4()
        target_vault_id = await self.api.resolve_vault_identifier(
            vault_id or self.api.config.server.default_active_vault
        )

        async with self.api.metastore.session() as session:
            job = BatchJob(
                id=job_id,
                vault_id=target_vault_id,
                status=BatchJobStatus.PENDING,
                notes_count=len(notes),
            )
            session.add(job)
            await session.commit()

        if background_tasks is not None:
            background_tasks.add_task(self._run_job, job_id, notes, target_vault_id, batch_size)
        else:
            task = asyncio.create_task(self._run_job(job_id, notes, target_vault_id, batch_size))
            self._active_tasks[job_id] = task
            task.add_done_callback(lambda t: self._task_done_callback(job_id, t))

        return job_id

    async def create_single_job(
        self,
        coro_fn: Callable[..., Awaitable[dict[str, Any]]],
        vault_id: UUID | str | None = None,
        background_tasks: Any | None = None,
        **kwargs: Any,
    ) -> UUID:
        """Create a tracked job that wraps a single async ingestion call.

        Unlike :meth:`create_job` (which processes a list of notes via
        ``ingest_batch_internal``), this method runs an arbitrary coroutine
        (e.g. ``api.ingest_from_url``) under the same job-tracking lifecycle
        so callers get a ``job_id`` they can poll.

        The *vault_id* is used both for the job record and forwarded to
        *coro_fn* (as ``vault_id=...``) so the ingestion targets the same vault.

        When *background_tasks* (a Starlette ``BackgroundTasks`` instance) is
        provided, the job is scheduled through it so that the ASGI server runs
        the task after the response is sent.  Otherwise an ``asyncio.Task`` is
        created directly.

        Args:
            coro_fn: An async callable (e.g. ``api.ingest_from_url``) that
                returns a dict result.
            vault_id: Optional vault identifier (resolved to UUID for job
                record and forwarded to *coro_fn*).
            background_tasks: Optional Starlette ``BackgroundTasks`` instance.
            **kwargs: Additional keyword arguments forwarded to *coro_fn*.

        Returns:
            UUID of the newly created job.
        """
        job_id = uuid4()
        target_vault_id = await self.api.resolve_vault_identifier(
            vault_id or self.api.config.server.default_active_vault
        )

        async with self.api.metastore.session() as session:
            job = BatchJob(
                id=job_id,
                vault_id=target_vault_id,
                status=BatchJobStatus.PENDING,
                notes_count=1,
            )
            session.add(job)
            await session.commit()

        # Forward vault_id to the coroutine alongside caller-provided kwargs
        coro_kwargs: dict[str, Any] = {'vault_id': vault_id, **kwargs}
        if background_tasks is not None:
            background_tasks.add_task(self._run_single_job, job_id, coro_fn, **coro_kwargs)
        else:
            task = asyncio.create_task(self._run_single_job(job_id, coro_fn, **coro_kwargs))
            self._active_tasks[job_id] = task
            task.add_done_callback(lambda t: self._task_done_callback(job_id, t))

        return job_id

    async def get_job_status(self, job_id: UUID) -> BatchJob | None:
        """Retrieve the current status of a job."""
        async with self.api.metastore.session() as session:
            return await session.get(BatchJob, job_id)

    async def reconcile_interrupted_jobs(self) -> int:
        """
        Identify jobs stuck in PROCESSING state (likely due to server restart)
        and mark them as FAILED.

        Returns:
            int: The number of jobs reconciled.
        """
        from sqlmodel import select, col

        count = 0
        async with self.api.metastore.session() as session:
            stmt = select(BatchJob).where(col(BatchJob.status) == BatchJobStatus.PROCESSING)
            stuck_jobs = (await session.exec(stmt)).all()

            for job in stuck_jobs:
                logger.warning(f'Reconciling interrupted batch job {job.id}')
                job.status = BatchJobStatus.FAILED
                job.completed_at = datetime.now(timezone.utc)
                job.error_info = (
                    'Job interrupted by server restart. Please resubmit pending documents.'
                )
                session.add(job)
                count += 1

            if count > 0:
                await session.commit()
                logger.info(f'Reconciled {count} interrupted batch jobs.')

        return count

    async def _get_job_for_update(self, session: Any, job_id: UUID) -> BatchJob | None:
        """Fetch a BatchJob row with SELECT ... FOR UPDATE to prevent race conditions.

        Args:
            session: Active async database session.
            job_id: The job UUID to fetch.

        Returns:
            The locked BatchJob instance, or None if not found.
        """
        from sqlmodel import select

        stmt = select(BatchJob).where(BatchJob.id == job_id).with_for_update()
        result = await session.exec(stmt)
        return result.first()

    async def _run_job(
        self, job_id: UUID, notes: list[Any], vault_id: UUID, batch_size: int = 32
    ) -> None:
        """Internal background task for job execution."""
        async with background_session('bg-batch'):
            logger.info(f'Starting batch job {job_id} ({len(notes)} notes)')

            try:
                # 1. Update status to PROCESSING
                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if not job:
                        logger.error(f'Job {job_id} not found in database.')
                        return
                    job.status = BatchJobStatus.PROCESSING
                    job.started_at = datetime.now(timezone.utc)
                    await session.commit()

                # 2. Execute Ingestion (Consuming Generator)
                # We iterate over chunks and update DB in real-time
                final_results = {}
                total_notes = len(notes)

                async for results in self.api.ingest_batch_internal(
                    notes=notes, vault_id=vault_id, batch_size=batch_size
                ):
                    final_results = results
                    processed = results.get('processed_count', 0)
                    failed = results.get('failed_count', 0)
                    skipped = results.get('skipped_count', 0)
                    total_done = processed + failed + skipped

                    async with self.api.metastore.session() as session:
                        job = await self._get_job_for_update(session, job_id)
                        if job:
                            job.processed_count = processed
                            job.failed_count = failed
                            job.skipped_count = skipped
                            job.progress = f'Processed {total_done}/{total_notes} notes'
                            await session.commit()

                # 3. Finalize Job Status
                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if not job:
                        return

                    job.status = BatchJobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    job.processed_count = final_results.get('processed_count', 0)
                    job.skipped_count = final_results.get('skipped_count', 0)
                    job.failed_count = final_results.get('failed_count', 0)
                    job.note_ids = final_results.get('note_ids', [])
                    job.error_info = final_results.get('errors')
                    job.progress = f'Completed: {total_notes}/{total_notes} processed'

                    await session.commit()
                    failed_count = job.failed_count or 0
                    if failed_count > 0:
                        errors = final_results.get('errors') or []
                        logger.warning(
                            'Batch job %s completed with %d failed chunks '
                            '(processed=%d, skipped=%d). First errors: %s',
                            job_id,
                            failed_count,
                            job.processed_count or 0,
                            job.skipped_count or 0,
                            errors[:3],
                        )
                    else:
                        logger.info(f'Batch job {job_id} completed successfully.')

            except Exception as e:
                logger.error(f'Batch job {job_id} failed: {e}', exc_info=True)
                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if job:
                        job.status = BatchJobStatus.FAILED
                        job.completed_at = datetime.now(timezone.utc)
                        job.error_info = str(e)
                        await session.commit()

    async def _run_single_job(
        self,
        job_id: UUID,
        coro_fn: Callable[..., Awaitable[dict[str, Any]]],
        **kwargs: Any,
    ) -> None:
        """Execute a single ingestion coroutine with job lifecycle tracking."""
        async with background_session('bg-ingest'):
            logger.info(f'Starting single job {job_id}')

            try:
                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if not job:
                        logger.error(f'Job {job_id} not found in database.')
                        return
                    job.status = BatchJobStatus.PROCESSING
                    job.started_at = datetime.now(timezone.utc)
                    await session.commit()

                result = await coro_fn(**kwargs)

                note_id = result.get('note_id')

                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if not job:
                        return
                    job.status = BatchJobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    job.processed_count = 1
                    job.note_ids = [note_id] if note_id else []
                    job.progress = 'Completed: 1/1 processed'
                    await session.commit()
                    logger.info(f'Single job {job_id} completed successfully.')

            except Exception as e:
                logger.error(f'Single job {job_id} failed: {e}', exc_info=True)
                async with self.api.metastore.session() as session:
                    job = await self._get_job_for_update(session, job_id)
                    if job:
                        job.status = BatchJobStatus.FAILED
                        job.completed_at = datetime.now(timezone.utc)
                        job.failed_count = 1
                        job.error_info = str(e)
                        await session.commit()
