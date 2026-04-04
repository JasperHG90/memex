"""Tests for vault summary integration with ingestion and truncation."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.config import VaultSummaryConfig
from memex_core.services.ingestion import IngestionService


def _make_ingestion_service(
    vault_summary_service=None,
    vault_summary_enabled=True,
):
    """Create an IngestionService with mocked dependencies."""
    metastore = MagicMock()
    filestore = MagicMock()
    lm = MagicMock()
    memory = MagicMock()
    file_processor = MagicMock()
    vaults = AsyncMock()
    vaults.resolve_vault_identifier = AsyncMock(return_value=uuid4())

    config = MagicMock()
    config.server.default_active_vault = 'global'
    config.server.vault_summary = VaultSummaryConfig(enabled=vault_summary_enabled)

    svc = IngestionService(
        metastore=metastore,
        filestore=filestore,
        config=config,
        lm=lm,
        memory=memory,
        file_processor=file_processor,
        vaults=vaults,
        vault_summary_service=vault_summary_service,
    )
    return svc


class TestIngestionPatchSummary:
    @pytest.mark.asyncio
    async def test_patch_summary_called_after_ingest(self):
        """AC-A03: After ingesting a note, patch_summary is triggered."""
        vault_summary = AsyncMock()
        vault_summary.patch_summary = AsyncMock()
        svc = _make_ingestion_service(vault_summary_service=vault_summary)

        note = MagicMock()
        note.idempotency_key = str(uuid4())
        note.content_fingerprint = 'abc123'
        note._content = b'test content'
        note._metadata = SimpleNamespace(
            name='Test Note',
            description='A test description',
            author=None,
            tags=[],
        )
        note._files = {}
        note.source_uri = None
        note.template = None

        vault_id = uuid4()
        svc._vaults.resolve_vault_identifier.return_value = vault_id

        # Mock the session for idempotency check
        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = None  # Note doesn't exist yet
        session.exec = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session.return_value = ctx

        # Mock transaction
        txn_session = AsyncMock()
        txn = MagicMock()
        txn.db_session = txn_session
        txn.save_file = AsyncMock()

        memory_result = {
            'chunks_created': 1,
            'facts_extracted': 1,
        }
        svc.memory.retain = AsyncMock(return_value=memory_result)

        with (
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new=AsyncMock(return_value='Test Note'),
            ),
            patch(
                'memex_core.services.ingestion._extract_date_from_frontmatter',
                return_value=datetime.now(timezone.utc),
            ),
            patch(
                'memex_core.services.ingestion.IngestionService._detect_overlapping_notes',
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=txn)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.ingest(note, vault_id=vault_id)

        assert result['status'] == 'success'

        # Give the background task a chance to run
        await asyncio.sleep(0.05)

        vault_summary.patch_summary.assert_called_once()
        call_args = vault_summary.patch_summary.call_args
        # Check vault_id was passed (positional or keyword)
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        assert vault_id in all_args

    @pytest.mark.asyncio
    async def test_patch_summary_not_called_when_disabled(self):
        """patch_summary is not triggered when vault_summary.enabled is False."""
        vault_summary = AsyncMock()
        svc = _make_ingestion_service(
            vault_summary_service=vault_summary,
            vault_summary_enabled=False,
        )

        note = MagicMock()
        note.idempotency_key = str(uuid4())
        note.content_fingerprint = 'abc123'
        note._content = b'test content'
        note._metadata = SimpleNamespace(name='Test Note', description='Desc', author=None, tags=[])
        note._files = {}
        note.source_uri = None
        note.template = None

        vault_id = uuid4()
        svc._vaults.resolve_vault_identifier.return_value = vault_id

        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = None
        session.exec = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session.return_value = ctx

        txn_session = AsyncMock()
        txn = MagicMock()
        txn.db_session = txn_session
        txn.save_file = AsyncMock()

        svc.memory.retain = AsyncMock(return_value={'chunks_created': 1})

        with (
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new=AsyncMock(return_value='Test Note'),
            ),
            patch(
                'memex_core.services.ingestion._extract_date_from_frontmatter',
                return_value=datetime.now(timezone.utc),
            ),
            patch(
                'memex_core.services.ingestion.IngestionService._detect_overlapping_notes',
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=txn)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await svc.ingest(note, vault_id=vault_id)

        await asyncio.sleep(0.05)
        vault_summary.patch_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_summary_not_called_without_service(self):
        """patch_summary is not triggered when no vault_summary_service is provided."""
        svc = _make_ingestion_service(vault_summary_service=None)

        note = MagicMock()
        note.idempotency_key = str(uuid4())
        note.content_fingerprint = 'abc123'
        note._content = b'test content'
        note._metadata = SimpleNamespace(name='Test Note', description='Desc', author=None, tags=[])
        note._files = {}
        note.source_uri = None
        note.template = None

        vault_id = uuid4()
        svc._vaults.resolve_vault_identifier.return_value = vault_id

        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = None
        session.exec = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session.return_value = ctx

        txn_session = AsyncMock()
        txn = MagicMock()
        txn.db_session = txn_session
        txn.save_file = AsyncMock()

        svc.memory.retain = AsyncMock(return_value={'chunks_created': 1})

        with (
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new=AsyncMock(return_value='Test Note'),
            ),
            patch(
                'memex_core.services.ingestion._extract_date_from_frontmatter',
                return_value=datetime.now(timezone.utc),
            ),
            patch(
                'memex_core.services.ingestion.IngestionService._detect_overlapping_notes',
                new=AsyncMock(return_value=[]),
            ),
        ):
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=txn)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # Should not raise
            result = await svc.ingest(note, vault_id=vault_id)

        assert result['status'] == 'success'


class TestPatchSummaryErrorHandling:
    @pytest.mark.asyncio
    async def test_patch_summary_error_does_not_propagate(self):
        """Errors in _patch_vault_summary_safe are logged, not raised."""
        svc = _make_ingestion_service()
        svc._vault_summary = AsyncMock()
        svc._vault_summary.patch_summary.side_effect = RuntimeError('LLM failed')

        # Should not raise
        await svc._patch_vault_summary_safe(
            vault_id=uuid4(),
            note_id=uuid4(),
            title='Test',
            description='Desc',
        )


class TestTruncateVaultCascade:
    @pytest.mark.asyncio
    async def test_truncate_deletes_vault_summary(self):
        """AC-A07: truncate_vault deletes the vault's VaultSummary."""
        from memex_core.services.vaults import VaultService

        metastore = MagicMock()
        filestore = AsyncMock()
        config = MagicMock()

        svc = VaultService(metastore=metastore, filestore=filestore, config=config)
        vault_id = uuid4()

        # Mock session
        session = AsyncMock()

        # Mock note query (no notes)
        note_result = MagicMock()
        note_result.all.return_value = []

        # Mock orphan entity query (no orphans)
        orphan_result = MagicMock()
        orphan_result.all.return_value = []

        # Mock delete results
        delete_result = MagicMock()
        delete_result.rowcount = 0

        session.exec = AsyncMock(side_effect=[note_result, orphan_result] + [delete_result] * 6)
        session.commit = AsyncMock()

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session.return_value = ctx

        counts = await svc.truncate_vault(vault_id)

        # Verify vault_summaries is in the counts
        assert 'vault_summaries' in counts
