import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

from memex_common.schemas import NoteCreateDTO

from memex_core.processing.batch import JobManager, OverlapError
from memex_core.memory.sql_models import BatchJob, BatchJobStatus


def _make_note_dto(
    name: str = 'n',
    description: str = 'd',
    content: bytes = b'hello',
) -> NoteCreateDTO:
    """Build a minimal valid NoteCreateDTO for use in unit tests.

    `NoteInput.calculate_idempotency_key_from_dto` requires a real DTO (not a
    MagicMock) because it instantiates `NoteInput` which validates `name` and
    `description` as `str` via Pydantic. The content is base64-encoded so the
    `Base64Bytes` validator accepts it; each test injects a unique value so the
    derived idempotency key differs per note instance.
    """
    return NoteCreateDTO(
        name=name,
        description=description,
        content=base64.b64encode(content),
    )


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


@pytest.fixture
def overlap_session(mock_metastore):
    """Configure the mock session so the overlap-check `SELECT` returns no rows
    (i.e. the new job proceeds to insert). Returns the (session, results_proxy)
    pair so individual tests can override `.first()` to simulate an overlap.

    The mock session shape mirrors a SQLAlchemy `AsyncSession` for the calls
    `JobManager.create_job` makes: `session.execute(...)` returns an awaitable
    `Result`, the result's `.first()` is sync.
    """
    # `session.execute(...)` returns a result-like object whose `.first()` is
    # synchronous and configurable per-test. The default mock_session.exec
    # path is unused by create_job (which calls .execute, not .exec).
    session = mock_metastore.session.return_value.__aenter__.return_value

    advisory_result = MagicMock()
    advisory_result.first.return_value = None
    overlap_result = MagicMock()
    overlap_result.first.return_value = None
    # session.execute is awaited; AsyncMock.side_effect lets us return a
    # different object per call. The first call is the advisory_xact_lock
    # SELECT, the second is the overlap-check SELECT.
    session.execute = AsyncMock(side_effect=[advisory_result, overlap_result])
    return session, overlap_result


@pytest.mark.asyncio
async def test_create_job(manager, mock_api, overlap_session):
    """Test job creation, persistence, and background task scheduling."""
    session, _ = overlap_session
    notes = [_make_note_dto(content=b'one')]
    vault_id = uuid4()
    resolved_vault_id = mock_api.resolve_vault_identifier.return_value

    with patch.object(manager, '_run_job', new_callable=AsyncMock) as mock_run_job:
        job_id = await manager.create_job(notes, vault_id)

    assert isinstance(job_id, UUID)
    session.add.assert_called()
    session.commit.assert_called()

    # Verify _run_job was invoked with the correct arguments to schedule the task
    mock_run_job.assert_called_once_with(job_id, notes, resolved_vault_id, 32)


# ---------------------------------------------------------------------------
# AC-020: idempotency-key derivation, overlap detection, advisory lock ordering
# ---------------------------------------------------------------------------


def _make_overlap_session(mock_metastore, *, overlap_row):
    """Helper for tests that customise the overlap-check `SELECT` result.

    `overlap_row` is the value returned by `result.first()` for the second
    `session.execute(...)` call (the overlap-check select). Pass `None` to
    indicate no overlap; pass a `(uuid, status_str)` tuple to simulate a hit.
    """
    session = mock_metastore.session.return_value.__aenter__.return_value
    advisory_result = MagicMock()
    advisory_result.first.return_value = None
    overlap_result = MagicMock()
    overlap_result.first.return_value = overlap_row
    session.execute = AsyncMock(side_effect=[advisory_result, overlap_result])
    return session


@pytest.mark.asyncio
async def test_create_job_computes_input_keys(manager, mock_api, mock_metastore):
    """AC-020: create_job must compute idempotency keys via
    `NoteInput.calculate_idempotency_key_from_dto` for every input note and
    pass the sorted-deduped list to the overlap query and the inserted row."""
    from memex_core.api import NoteInput

    notes = [
        _make_note_dto(content=b'alpha'),
        _make_note_dto(content=b'beta'),
        _make_note_dto(content=b'alpha'),  # duplicate — must be deduped
    ]
    expected_keys = sorted({NoteInput.calculate_idempotency_key_from_dto(n) for n in notes})

    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job(notes, vault_id=uuid4())

    # The overlap-query is the second execute call. Inspect its bound params
    # to assert the keys list matches what we computed.
    overlap_call = session.execute.call_args_list[1]
    _stmt, params = overlap_call.args
    assert params['keys'] == expected_keys, (
        f'expected sorted-deduped keys {expected_keys}, got {params["keys"]}'
    )
    # Sanity: dedup actually happens (3 notes → 2 unique keys here).
    assert len(params['keys']) == 2


@pytest.mark.asyncio
async def test_create_job_stores_input_note_keys(manager, mock_api, mock_metastore):
    """AC-020: the inserted BatchJob row carries `input_note_keys=<sorted set>`."""
    from memex_core.api import NoteInput

    notes = [_make_note_dto(content=b'x'), _make_note_dto(content=b'y')]
    expected_keys = sorted({NoteInput.calculate_idempotency_key_from_dto(n) for n in notes})

    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job(notes, vault_id=uuid4())

    # The job is added via session.add(...) — assert the BatchJob carries the keys.
    added_objs = [c.args[0] for c in session.add.call_args_list]
    batch_jobs = [o for o in added_objs if isinstance(o, BatchJob)]
    assert len(batch_jobs) == 1
    assert batch_jobs[0].input_note_keys == expected_keys


@pytest.mark.asyncio
async def test_create_job_detects_pending_overlap(manager, mock_api, mock_metastore):
    """AC-020: when the overlap-check SELECT returns a PENDING job, raise
    `OverlapError` carrying the existing job's id and status; do not insert."""
    existing_id = uuid4()
    session = _make_overlap_session(
        mock_metastore, overlap_row=(existing_id, BatchJobStatus.PENDING.value)
    )

    notes = [_make_note_dto(content=b'p1'), _make_note_dto(content=b'p2')]

    with pytest.raises(OverlapError) as excinfo:
        await manager.create_job(notes, vault_id=uuid4())

    err = excinfo.value
    assert err.existing_id == existing_id
    assert err.status == BatchJobStatus.PENDING.value
    # Forward-compat field present, currently empty.
    assert err.overlapping_keys == []
    # Crucially: no row was inserted.
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_job_detects_processing_overlap(manager, mock_api, mock_metastore):
    """AC-020: same as above but for PROCESSING — both statuses must trigger
    OverlapError. The status filter `IN ('pending', 'processing')` lives in SQL;
    here we simulate the row coming back from that filter."""
    existing_id = uuid4()
    session = _make_overlap_session(
        mock_metastore, overlap_row=(existing_id, BatchJobStatus.PROCESSING.value)
    )

    notes = [_make_note_dto(content=b'q')]

    with pytest.raises(OverlapError) as excinfo:
        await manager.create_job(notes, vault_id=uuid4())

    assert excinfo.value.existing_id == existing_id
    assert excinfo.value.status == BatchJobStatus.PROCESSING.value
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_job_ignores_completed_overlaps(manager, mock_api, mock_metastore):
    """AC-020: COMPLETED jobs do not block new submissions. The status filter is
    `IN ('pending', 'processing')` in SQL; in the unit test we model that by
    returning no overlap row (since SQL would have filtered the completed job
    out). Assert the overlap query was emitted with the correct status filter."""
    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job([_make_note_dto(content=b'c')], vault_id=uuid4())

    # The overlap query's SQL must filter status to pending|processing only.
    overlap_call = session.execute.call_args_list[1]
    overlap_sql = str(overlap_call.args[0])
    assert "status IN ('pending', 'processing')" in overlap_sql, (
        'overlap query must filter status to pending|processing — completed/failed '
        'jobs must not block new submissions.'
    )
    # And the row was inserted (no OverlapError).
    session.add.assert_called()


@pytest.mark.asyncio
async def test_create_job_ignores_failed_overlaps(manager, mock_api, mock_metastore):
    """AC-020: FAILED jobs do not block new submissions — same SQL guard as
    completed. Re-asserted here so a future change to the status filter that
    accidentally re-includes terminal statuses fails one of these two tests."""
    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job([_make_note_dto(content=b'f')], vault_id=uuid4())

    overlap_call = session.execute.call_args_list[1]
    overlap_sql = str(overlap_call.args[0])
    assert 'failed' not in overlap_sql.lower(), (
        'overlap query must not include FAILED jobs — only PENDING|PROCESSING.'
    )
    session.add.assert_called()


@pytest.mark.asyncio
async def test_create_job_overlap_is_vault_scoped(manager, mock_api, mock_metastore):
    """AC-020: the overlap query filters by vault_id. Single-thread variant —
    we assert the SQL contains `WHERE vault_id = :vault_id` and the bound
    parameter matches the resolved vault id (not the raw input)."""
    resolved = uuid4()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=resolved)

    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job([_make_note_dto(content=b'v')], vault_id='global')

    overlap_call = session.execute.call_args_list[1]
    sql = str(overlap_call.args[0])
    params = overlap_call.args[1]
    assert 'vault_id = :vault_id' in sql, 'overlap query must filter by vault_id'
    assert params['vault_id'] == resolved, (
        'overlap query must scope by the resolved vault UUID, not the raw input'
    )


@pytest.mark.asyncio
async def test_create_job_overlap_on_any_key_match(manager, mock_api, mock_metastore):
    """AC-020: any single overlapping key triggers OverlapError — the SQL uses
    `EXISTS (SELECT 1 FROM unnest(...) WHERE input_note_keys @> jsonb_build_array(k))`
    so a partial overlap is enough. We assert the SQL shape (so the dev can't
    accidentally substitute `@>` with `=`) and that any returned row raises."""
    existing_id = uuid4()
    session = _make_overlap_session(
        mock_metastore, overlap_row=(existing_id, BatchJobStatus.PENDING.value)
    )

    # Three notes -> three keys; partial overlap with the existing job is enough.
    notes = [
        _make_note_dto(content=b'partial-1'),
        _make_note_dto(content=b'partial-2'),
        _make_note_dto(content=b'partial-3'),
    ]

    with pytest.raises(OverlapError):
        await manager.create_job(notes, vault_id=uuid4())

    overlap_call = session.execute.call_args_list[1]
    sql = str(overlap_call.args[0])
    assert 'unnest' in sql.lower(), 'overlap query must iterate the input keys via unnest'
    assert '@>' in sql, 'overlap query must use the @> containment operator (jsonb_path_ops index)'


@pytest.mark.asyncio
async def test_create_job_acquires_advisory_lock_before_select(manager, mock_api, mock_metastore):
    """AC-020 (rev) load-bearing: TOCTOU close depends on the advisory lock
    being acquired *before* the overlap-check SELECT. A future maintainer
    reordering these calls reopens the race — this test fails if the lock
    statement is not the first execute() invocation."""
    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job([_make_note_dto(content=b'lock')], vault_id=uuid4())

    # The SQL trace: `session.execute` is called twice in order.
    assert len(session.execute.call_args_list) == 2, (
        'create_job must emit exactly 2 SQL statements before insert: '
        'the advisory lock acquisition and the overlap-check SELECT.'
    )
    first_sql = str(session.execute.call_args_list[0].args[0])
    second_sql = str(session.execute.call_args_list[1].args[0])
    assert 'pg_advisory_xact_lock' in first_sql, (
        'first SQL statement must be `pg_advisory_xact_lock(...)` — the TOCTOU '
        'close depends on the lock being acquired before the overlap query.'
    )
    assert 'hashtext' in first_sql and '::text' in first_sql, (
        'advisory-lock key must be hashtext over the vault_id cast to text; '
        'hashtext requires text input but vault_id is a UUID, hence the cast.'
    )
    assert 'batch_jobs' in second_sql.lower() and '@>' in second_sql, (
        'second SQL statement must be the overlap-check SELECT against batch_jobs.'
    )


@pytest.mark.asyncio
async def test_create_job_advisory_lock_uses_resolved_vault_id(manager, mock_api, mock_metastore):
    """AC-020 (rev): the advisory-lock key is bound to the *resolved* vault UUID
    (str cast for `hashtext`), not the raw input or the default-vault name. If
    the binding is wrong, two callers passing the same vault under different
    aliases would not serialize correctly."""
    resolved = uuid4()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=resolved)

    session = _make_overlap_session(mock_metastore, overlap_row=None)

    with patch.object(manager, '_run_job', new_callable=AsyncMock):
        await manager.create_job([_make_note_dto(content=b'a')], vault_id='global')

    advisory_call = session.execute.call_args_list[0]
    params = advisory_call.args[1]
    assert params == {'vault_id': str(resolved)}, (
        f'advisory_xact_lock key must be the resolved vault UUID as a string; got params={params!r}'
    )


@pytest.mark.asyncio
async def test_run_job_success(manager, mock_api, mock_session):
    """Test background job execution success path."""
    job_id = uuid4()
    vault_id = uuid4()
    notes = [MagicMock()]

    # Mock job retrieval (via SELECT ... FOR UPDATE)
    job = BatchJob(
        id=job_id, vault_id=vault_id, status=BatchJobStatus.PENDING, notes_count=len(notes)
    )
    mock_session.exec.return_value.first.return_value = job

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
    mock_session.exec.return_value.first.return_value = job

    # Mock API ingestion to raise exception when iterated
    async def mock_ingest_gen_fail(*args, **kwargs):
        raise Exception('Fatal Error')
        yield {}

    mock_api.ingest_batch_internal.side_effect = mock_ingest_gen_fail

    await manager._run_job(job_id, notes, vault_id)

    assert job.status == BatchJobStatus.FAILED
    assert 'Fatal Error' in job.error_info
    mock_session.commit.assert_called()


@pytest.mark.asyncio
async def test_run_job_partial_failure_logs_warning(manager, mock_api, mock_session, caplog):
    """Regression for issue #41 residual: when ingest_batch_internal yields a final result
    with failed_count > 0, the batch manager must log a warning rather than the
    unconditional 'completed successfully' info message. Job status stays COMPLETED
    (no new status value), but operators now have a loud signal."""
    import logging

    job_id = uuid4()
    vault_id = uuid4()
    notes = [MagicMock(), MagicMock()]

    job = BatchJob(
        id=job_id, vault_id=vault_id, status=BatchJobStatus.PENDING, notes_count=len(notes)
    )
    mock_session.exec.return_value.first.return_value = job

    async def mock_ingest_gen(*args, **kwargs):
        yield {
            'processed_count': 1,
            'skipped_count': 0,
            'failed_count': 1,
            'note_ids': [str(uuid4())],
            'errors': [{'chunk_start': 1, 'error': 'simulated chunk failure'}],
        }

    mock_api.ingest_batch_internal.side_effect = mock_ingest_gen

    with caplog.at_level(logging.INFO, logger='memex.core.processing.batch'):
        await manager._run_job(job_id, notes, vault_id)

    assert job.status == BatchJobStatus.COMPLETED
    assert job.failed_count == 1

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    info_success_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and 'completed successfully' in r.getMessage()
    ]
    assert warning_records, 'expected a WARNING about partial failures'
    assert not info_success_records, (
        'must not emit the "completed successfully" info log when failed_count > 0'
    )
    assert '1 failed chunks' in warning_records[0].getMessage()
    assert 'simulated chunk failure' in warning_records[0].getMessage()
