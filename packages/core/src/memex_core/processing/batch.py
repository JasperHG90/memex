import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import sqlalchemy as sa

from memex_core.context import background_session
from memex_core.memory.sql_models import BatchJob, BatchJobStatus

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.processing.batch')


class OverlapError(Exception):
    """Raised when a new batch overlaps an in-flight job in the same vault.

    Surfaced by ``JobManager.create_job`` when the incoming batch's idempotency
    keys overlap any ``PENDING`` or ``PROCESSING`` job's stored ``input_note_keys``
    in the same vault. The HTTP layer translates this to a 409 with a ``Location``
    header pointing at the existing job.

    ``overlapping_keys`` is a forward-compatible field carrying the subset of keys
    that overlapped. It is currently always ``[]`` (subset computation is deferred
    per RFC-002 §A4); future code may populate it without changing this signature.
    """

    def __init__(
        self,
        existing_id: UUID,
        status: str,
        overlapping_keys: list[str] | None = None,
    ) -> None:
        self.existing_id = existing_id
        self.status = status
        self.overlapping_keys = overlapping_keys if overlapping_keys is not None else []
        super().__init__(
            f'Batch overlaps with in-flight job {existing_id} '
            f'(status={status}, overlap={len(self.overlapping_keys)} keys)'
        )


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

        Idempotency keys are derived from the incoming DTOs via
        ``NoteInput.calculate_idempotency_key_from_dto`` and stored on the row's
        ``input_note_keys`` column. Before insert we acquire a per-vault
        ``pg_advisory_xact_lock`` and query for any ``PENDING`` or ``PROCESSING``
        job in the same vault whose ``input_note_keys`` overlaps the incoming
        batch — if found, ``OverlapError`` is raised so the caller can return
        409 + ``Location`` instead of starting a duplicate. The advisory lock
        is auto-released at COMMIT/ROLLBACK and is keyed per-vault so unrelated
        vaults' submissions proceed in parallel. (RFC-002 §"Step 2-3".)

        Args:
            notes: List of NoteDTOs to ingest.
            vault_id: Optional target vault identifier.
            batch_size: Processing chunk size.
            background_tasks: Optional Starlette ``BackgroundTasks`` instance.

        Returns:
            UUID: The created Job ID.

        Raises:
            OverlapError: When an in-flight job in the same vault has any
                idempotency key in common with this batch.
        """
        # Local import: NoteInput pulls in the rest of api.py, which imports
        # back into this module; defer to runtime to avoid the cycle.
        from memex_core.api import NoteInput

        job_id = uuid4()
        target_vault_id = await self.api.resolve_vault_identifier(
            vault_id or self.api.config.server.default_active_vault
        )

        # Compute idempotency keys at the storage boundary. Sorted+deduped so the
        # stored array has no duplicates and the @> probes are stable across input
        # orderings. (RFC-002 §"Idempotency-key derivation: where".)
        input_keys = sorted({NoteInput.calculate_idempotency_key_from_dto(n) for n in notes})

        async with self.api.metastore.session() as session:
            # AC-020 (rev) TOCTOU protection: serialize per-vault create_job
            # transactions using a Postgres advisory transaction lock. The lock
            # is released automatically at COMMIT/ROLLBACK; no explicit unlock
            # needed. Per-vault scope (hashtext over the UUID's text repr) so
            # unrelated vaults' submissions don't serialize against each other.
            # Without this lock, two concurrent calls can both pass the overlap
            # check before either INSERTs — both succeed, defeating the AC.
            #
            # `hashtext` returns int4 (32-bit). At Memex's expected vault count
            # the birthday-bound collision probability is negligible; if the
            # deployment ever grows past ~65k vaults we can swap to
            # `hashtextextended` (PG ≥ 11, returns int8) — already feasible
            # because migration 021 guards PG ≥ 11.
            await session.execute(
                sa.text('SELECT pg_advisory_xact_lock(hashtext(:vault_id::text))'),
                {'vault_id': str(target_vault_id)},
            )

            # Overlap query: find any PENDING/PROCESSING job in the same vault
            # whose input_note_keys jsonb-array contains any of the incoming
            # keys. Inner subquery iterates :keys via unnest and probes
            # `input_note_keys @> jsonb_build_array(k)` — the `@>` operator is
            # supported by the `jsonb_path_ops` GIN index on this column
            # (migration 021), so the per-key probe is index-aided.
            overlap_stmt = sa.text(
                """
                SELECT id, status
                FROM batch_jobs
                WHERE vault_id = :vault_id
                  AND status IN ('pending', 'processing')
                  AND EXISTS (
                      SELECT 1 FROM unnest(CAST(:keys AS text[])) AS k
                      WHERE input_note_keys @> jsonb_build_array(k)
                  )
                LIMIT 1
                """
            )
            result = await session.execute(
                overlap_stmt,
                {'vault_id': target_vault_id, 'keys': input_keys},
            )
            row = result.first()
            if row is not None:
                existing_id, status_str = row
                # subset computation deferred — see RFC-002 §A4.
                raise OverlapError(
                    existing_id=existing_id,
                    status=status_str,
                    overlapping_keys=[],
                )

            job = BatchJob(
                id=job_id,
                vault_id=target_vault_id,
                status=BatchJobStatus.PENDING,
                notes_count=len(notes),
                input_note_keys=input_keys,
            )
            session.add(job)
            await session.commit()  # releases pg_advisory_xact_lock

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
