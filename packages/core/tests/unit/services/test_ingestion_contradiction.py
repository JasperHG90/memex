"""Unit test: verify contradiction_task remains in result dict after ingest()."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.services.ingestion import IngestionService
from memex_core.services.vaults import VaultService


@pytest.fixture
def ingestion_service(mock_metastore, mock_filestore, mock_config):
    memory = AsyncMock()
    lm = MagicMock()
    file_processor = MagicMock()
    vaults = MagicMock(spec=VaultService)
    vaults.resolve_vault_identifier = AsyncMock(return_value=uuid4())

    svc = IngestionService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
        lm=lm,
        memory=memory,
        file_processor=file_processor,
        vaults=vaults,
    )
    svc._audit_service = MagicMock()
    svc._audit_service.log = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_ingest_preserves_contradiction_task_in_result(ingestion_service, mock_session):
    """After ingest(), the result dict must still contain 'contradiction_task'.

    The service layer must NOT pop or await the contradiction_task coroutine.
    Downstream handlers (_schedule_contradiction, batch.py) rely on finding
    this key in the result dict so they can run contradiction detection
    after the transaction has committed.
    """
    from memex_core.memory.sql_models import Vault

    sentinel_coro = AsyncMock()()
    note_id = uuid4()

    ingestion_service.memory.retain = AsyncMock(
        return_value={
            'status': 'success',
            'contradiction_task': sentinel_coro,
        }
    )

    note = MagicMock()
    note.idempotency_key = note_id
    note._metadata.name = f'Test Note {note_id}'
    note._metadata.description = 'desc'
    note._metadata.author = None
    note._metadata.tags = []
    note._content = b'# Test content with unique id ' + str(note_id).encode()
    note._files = {}
    note.source_uri = None
    note.content_fingerprint = 'fp_' + str(note_id)
    note.template = None

    # Idempotency check: no existing note
    mock_session.exec.return_value.first.return_value = None

    mock_vault = MagicMock(spec=Vault)
    mock_vault.name = 'test-vault'
    mock_session.get = AsyncMock(return_value=mock_vault)

    with (
        patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
        ) as mock_title,
        patch('memex_core.services.ingestion.audit_event'),
    ):
        ctx = AsyncMock()
        ctx.db_session = mock_session
        mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_title.return_value = 'Resolved Title'

        ingestion_service._detect_overlapping_notes = AsyncMock(return_value=[])

        result = await ingestion_service.ingest(
            note, vault_id=uuid4(), event_date=datetime.now(timezone.utc)
        )

    # Core assertion: contradiction_task key is preserved
    assert 'contradiction_task' in result, (
        'contradiction_task was popped from result dict by the service layer'
    )
    assert result['contradiction_task'] is sentinel_coro

    # Verify other result fields are still correct
    assert result['note_id'] == note_id
    assert result['status'] == 'success'

    # Clean up the coroutine to avoid RuntimeWarning
    sentinel_coro.close()


@pytest.mark.asyncio
async def test_process_chunk_closes_contradiction_coroutine(ingestion_service, mock_session):
    """_process_chunk() must pop contradiction_task and call .close() on it.

    Unlike ingest(), _process_chunk() returns list[str] and has no downstream
    handler for contradiction tasks. The coroutine must be explicitly closed
    to prevent RuntimeWarning about unawaited coroutines.
    """
    sentinel_coro = MagicMock()
    note_id = uuid4()

    ingestion_service.memory.retain = AsyncMock(
        return_value={
            'status': 'success',
            'contradiction_task': sentinel_coro,
        }
    )

    # Build a minimal note DTO that passes _needs_conversion() == False
    note_dto = MagicMock()
    note_dto.filename = None  # no filename => _needs_conversion returns False
    note_dto.content_decoded = b'# Batch note content ' + str(note_id).encode()
    note_dto.name = f'Batch Note {note_id}'
    note_dto.description = 'batch desc'
    note_dto.author = None
    note_dto.tags = []
    note_dto.files = {}
    note_dto.user_notes = None

    chunk = [(0, note_dto, note_id)]
    note_fingerprints = ['fp_' + str(note_id)]
    target_vault_id = uuid4()

    with (
        patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
        ) as mock_title,
    ):
        ctx = AsyncMock()
        ctx.db_session = mock_session
        mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_title.return_value = 'Batch Title'

        result = await ingestion_service._process_chunk(
            chunk=chunk,
            vault_name='test-vault',
            note_fingerprints=note_fingerprints,
            target_vault_id=target_vault_id,
        )

    # The coroutine must have been closed (not awaited)
    sentinel_coro.close.assert_called_once()

    # _process_chunk returns list[str] of processed IDs
    assert result == [str(note_id)]
