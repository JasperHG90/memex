"""Unit tests: verify contradiction handling in IngestionService."""

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


def _make_note():
    """Create a minimal mock note for testing."""

    note_id = uuid4()
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
    return note, note_id


@pytest.mark.asyncio
async def test_ingest_awaits_contradiction_task(ingestion_service, mock_session):
    """After ingest(), the contradiction coroutine must be awaited (not preserved in result)."""
    from memex_core.memory.sql_models import Vault

    tracked = {'awaited': False}

    async def fake_contradiction():
        tracked['awaited'] = True

    note, note_id = _make_note()

    ingestion_service.memory.retain = AsyncMock(
        return_value={
            'status': 'success',
            'contradiction_task': fake_contradiction(),
        }
    )

    mock_vault = MagicMock(spec=Vault)
    mock_vault.name = 'test-vault'
    mock_session.get = AsyncMock(return_value=mock_vault)
    mock_session.exec.return_value.first.return_value = None

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

    # Contradiction task must have been awaited
    assert tracked['awaited'], 'contradiction coroutine was not awaited'
    # Key must be removed from result
    assert 'contradiction_task' not in result


@pytest.mark.asyncio
async def test_ingest_handles_none_contradiction_task(ingestion_service, mock_session):
    """ingest() handles result dict without contradiction_task (no error)."""
    from memex_core.memory.sql_models import Vault

    note, note_id = _make_note()

    ingestion_service.memory.retain = AsyncMock(
        return_value={
            'status': 'success',
        }
    )

    mock_vault = MagicMock(spec=Vault)
    mock_vault.name = 'test-vault'
    mock_session.get = AsyncMock(return_value=mock_vault)
    mock_session.exec.return_value.first.return_value = None

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

    assert result['status'] == 'success'
    assert result['note_id'] == note_id


@pytest.mark.asyncio
async def test_process_chunk_collects_and_awaits_contradictions(ingestion_service, mock_session):
    """_process_chunk() must collect contradiction coroutines and await them all after commit."""
    tracked = {'count': 0}

    async def fake_contradiction():
        tracked['count'] += 1

    note_ids = [uuid4(), uuid4()]

    # Each retain call returns a different contradiction coroutine
    ingestion_service.memory.retain = AsyncMock(
        side_effect=[
            {'status': 'success', 'contradiction_task': fake_contradiction()},
            {'status': 'success', 'contradiction_task': fake_contradiction()},
        ]
    )

    note_dtos = []
    for nid in note_ids:
        note_dto = MagicMock()
        note_dto.filename = None
        note_dto.content_decoded = b'# Content ' + str(nid).encode()
        note_dto.name = f'Note {nid}'
        note_dto.description = 'desc'
        note_dto.author = None
        note_dto.tags = []
        note_dto.files = {}
        note_dto.user_notes = None
        note_dtos.append(note_dto)

    chunk = [(i, dto, nid) for i, (dto, nid) in enumerate(zip(note_dtos, note_ids))]
    note_fingerprints = [f'fp_{nid}' for nid in note_ids]
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

    # Both coroutines must have been awaited
    assert tracked['count'] == 2, f'Expected 2 contradictions awaited, got {tracked["count"]}'
    assert result == [str(nid) for nid in note_ids]


@pytest.mark.asyncio
async def test_process_chunk_handles_mixed_none_tasks(ingestion_service, mock_session):
    """_process_chunk() handles mix of None and non-None contradiction tasks."""
    tracked = {'count': 0}

    async def fake_contradiction():
        tracked['count'] += 1

    note_ids = [uuid4(), uuid4(), uuid4()]

    ingestion_service.memory.retain = AsyncMock(
        side_effect=[
            {'status': 'success', 'contradiction_task': fake_contradiction()},
            {'status': 'success'},  # No contradiction_task key
            {'status': 'success', 'contradiction_task': None},
        ]
    )

    note_dtos = []
    for nid in note_ids:
        note_dto = MagicMock()
        note_dto.filename = None
        note_dto.content_decoded = b'# Content ' + str(nid).encode()
        note_dto.name = f'Note {nid}'
        note_dto.description = 'desc'
        note_dto.author = None
        note_dto.tags = []
        note_dto.files = {}
        note_dto.user_notes = None
        note_dtos.append(note_dto)

    chunk = [(i, dto, nid) for i, (dto, nid) in enumerate(zip(note_dtos, note_ids))]
    note_fingerprints = [f'fp_{nid}' for nid in note_ids]
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

    # Only the one real coroutine should have been awaited
    assert tracked['count'] == 1
    assert len(result) == 3
