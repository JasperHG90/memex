"""Tests for document date -> event_date resolution in ingestion methods.

Verifies the three-strategy chain:
1. document_date from extraction metadata (web date / file mtime)
2. LLM fallback via extract_document_date
3. Final fallback to datetime.now(UTC)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.api import NoteInput
from memex_core.processing.models import ExtractedContent
from memex_core.services.ingestion import IngestionService


def _make_extracted(document_date: datetime | None = None) -> ExtractedContent:
    """Helper to build an ExtractedContent with optional document_date."""
    return ExtractedContent(
        content='Article body text.',
        source='https://example.com/article',
        content_type='web',
        metadata={
            'title': 'Test Article',
            'date': '2023-06-15' if document_date else None,
            'author': 'Author',
            'url': 'https://example.com/article',
            'hostname': 'example.com',
        },
        document_date=document_date,
    )


class TestIngestFromUrlEventDate:
    """Test event_date resolution in ingest_from_url."""

    @pytest.mark.asyncio
    async def test_uses_web_metadata_date(self, api):
        """When trafilatura provides a date, it should be used as event_date."""
        web_date = datetime(2023, 6, 15, tzinfo=timezone.utc)
        extracted = _make_extracted(document_date=web_date)

        with (
            patch(
                'memex_core.services.ingestion.WebContentProcessor.fetch_and_extract',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ) as mock_ingest,
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
        ):
            await api.ingest_from_url('https://example.com/article')

            mock_ingest.assert_called_once()
            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == web_date

    @pytest.mark.asyncio
    async def test_falls_back_to_llm_when_no_metadata_date(self, api):
        """When no web metadata date, LLM fallback should be tried."""
        extracted = _make_extracted(document_date=None)
        llm_date = datetime(2022, 3, 10, tzinfo=timezone.utc)

        with (
            patch(
                'memex_core.services.ingestion.WebContentProcessor.fetch_and_extract',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=llm_date,
            ) as mock_extract_date,
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ) as mock_ingest,
        ):
            await api.ingest_from_url('https://example.com/article')

            mock_extract_date.assert_called_once()
            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == llm_date

    @pytest.mark.asyncio
    async def test_falls_back_to_none_when_all_strategies_fail(self, api):
        """When both metadata and LLM return None, event_date should be None."""
        extracted = _make_extracted(document_date=None)

        with (
            patch(
                'memex_core.services.ingestion.WebContentProcessor.fetch_and_extract',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ) as mock_ingest,
        ):
            await api.ingest_from_url('https://example.com/article')

            _, kwargs = mock_ingest.call_args
            # event_date is None; ingest() will use datetime.now(UTC)
            assert kwargs['event_date'] is None

    @pytest.mark.asyncio
    async def test_skips_llm_when_metadata_date_present(self, api):
        """When document_date is set, LLM extraction should NOT be called."""
        web_date = datetime(2023, 6, 15, tzinfo=timezone.utc)
        extracted = _make_extracted(document_date=web_date)

        with (
            patch(
                'memex_core.services.ingestion.WebContentProcessor.fetch_and_extract',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
            ) as mock_extract_date,
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ),
        ):
            await api.ingest_from_url('https://example.com/article')
            mock_extract_date.assert_not_called()


class TestIngestFromFileEventDate:
    """Test event_date resolution in ingest_from_file for non-markdown files."""

    @pytest.mark.asyncio
    async def test_uses_file_mtime_date(self, api, tmp_path):
        """When file mtime is available, it should be used as event_date."""
        test_file = tmp_path / 'doc.pdf'
        test_file.write_bytes(b'fake pdf content')

        file_date = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        extracted = ExtractedContent(
            content='PDF content as markdown.',
            source=str(test_file),
            content_type='pdf',
            metadata={},
            document_date=file_date,
        )

        with (
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ) as mock_ingest,
        ):
            api._ingestion._file_processor = MagicMock()
            api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

            await api.ingest_from_file(test_file)

            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == file_date

    @pytest.mark.asyncio
    async def test_falls_back_to_llm_for_file(self, api, tmp_path):
        """When file has no mtime date, LLM fallback should be used."""
        test_file = tmp_path / 'doc.docx'
        test_file.write_bytes(b'fake docx content')

        extracted = ExtractedContent(
            content='Document content.',
            source=str(test_file),
            content_type='docx',
            metadata={},
            document_date=None,
        )

        llm_date = datetime(2021, 8, 20, tzinfo=timezone.utc)

        with (
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=llm_date,
            ) as mock_extract_date,
            patch.object(
                IngestionService,
                'ingest',
                new_callable=AsyncMock,
                return_value={'status': 'success'},
            ) as mock_ingest,
        ):
            api._ingestion._file_processor = MagicMock()
            api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

            await api.ingest_from_file(test_file)

            mock_extract_date.assert_called_once()
            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == llm_date


class TestIngestEventDateToRetainContent:
    """Test that ingest() correctly passes event_date to RetainContent."""

    @pytest.mark.asyncio
    async def test_ingest_uses_provided_event_date(self, api):
        """When event_date is provided, it should be used in RetainContent."""
        custom_date = datetime(2020, 5, 1, tzinfo=timezone.utc)

        note = NoteInput(
            name='Test NoteInput',
            description='A test note',
            content=b'# Test\nContent here.',
            tags=['test'],
        )

        mock_memory = AsyncMock()
        mock_memory.retain.return_value = {'unit_ids': ['abc']}

        with (
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_class,
        ):
            mock_txn = AsyncMock()
            mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn.__aexit__ = AsyncMock(return_value=False)
            mock_txn.db_session = AsyncMock()
            mock_txn_class.return_value = mock_txn

            api._ingestion.memory = mock_memory

            await api.ingest(note, event_date=custom_date)

            mock_memory.retain.assert_called_once()
            _, kwargs = mock_memory.retain.call_args
            retain_content = kwargs['contents'][0]
            assert retain_content.event_date == custom_date

    @pytest.mark.asyncio
    async def test_ingest_defaults_to_now_when_no_event_date(self, api):
        """When event_date is None, RetainContent should use datetime.now(UTC)."""
        note = NoteInput(
            name='Test NoteInput',
            description='A test note',
            content=b'# Test\nContent here.',
            tags=['test'],
        )

        mock_memory = AsyncMock()
        mock_memory.retain.return_value = {'unit_ids': ['abc']}

        with (
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_class,
        ):
            mock_txn = AsyncMock()
            mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn.__aexit__ = AsyncMock(return_value=False)
            mock_txn.db_session = AsyncMock()
            mock_txn_class.return_value = mock_txn

            api._ingestion.memory = mock_memory

            before = datetime.now(timezone.utc)
            await api.ingest(note, event_date=None)
            after = datetime.now(timezone.utc)

            mock_memory.retain.assert_called_once()
            _, kwargs = mock_memory.retain.call_args
            retain_content = kwargs['contents'][0]
            # Should be approximately now
            assert before <= retain_content.event_date <= after
