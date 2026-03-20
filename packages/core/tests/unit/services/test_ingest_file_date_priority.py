"""Tests for BUG-2: LLM date extraction was skipped for non-PDF files.

The date resolution priority for ingest_from_file should be:
1. LLM content extraction (always attempted)
2. PDF metadata creation date
3. File processor's document_date (mtime)
4. Final fallback to now()

Previously, when markitdown set document_date to file mtime (always present),
the LLM extraction was gated behind `if extracted.document_date is None`
and never called.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.processing.models import ExtractedContent
from memex_core.services.ingestion import IngestionService


class TestLlmDateAlwaysAttempted:
    """LLM extraction must always be attempted, even when file mtime exists."""

    @pytest.mark.asyncio
    async def test_llm_called_when_mtime_present(self, api, tmp_path):
        """The core bug: mtime is present but LLM should still be called."""
        test_file = tmp_path / 'report.docx'
        test_file.write_bytes(b'fake content')

        file_mtime = datetime(2026, 3, 10, tzinfo=timezone.utc)
        llm_date = datetime(2022, 3, 1, tzinfo=timezone.utc)

        extracted = ExtractedContent(
            content='This report was written in March 2022.',
            source=str(test_file),
            content_type='docx',
            metadata={},
            document_date=file_mtime,  # mtime is present
        )

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
            ) as mock_extract,
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

            # LLM extraction MUST be called regardless of mtime
            mock_extract.assert_called_once()
            _, kwargs = mock_ingest.call_args
            # LLM date should win over mtime
            assert kwargs['event_date'] == llm_date

    @pytest.mark.asyncio
    async def test_falls_back_to_mtime_when_llm_returns_none(self, api, tmp_path):
        """When LLM extraction returns None, fall back to file mtime."""
        test_file = tmp_path / 'data.csv'
        test_file.write_bytes(b'col1,col2\n1,2')

        file_mtime = datetime(2025, 6, 20, tzinfo=timezone.utc)

        extracted = ExtractedContent(
            content='col1,col2\n1,2',
            source=str(test_file),
            content_type='csv',
            metadata={},
            document_date=file_mtime,
        )

        with (
            patch(
                'memex_core.services.vaults.VaultService.resolve_vault_identifier',
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_extract,
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

            mock_extract.assert_called_once()
            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == file_mtime

    @pytest.mark.asyncio
    async def test_pdf_creation_date_used_when_llm_returns_none(self, api, tmp_path):
        """For PDFs, creation_date metadata should be used after LLM fails."""
        test_file = tmp_path / 'document.pdf'
        test_file.write_bytes(b'fake pdf')

        pdf_creation = datetime(2023, 11, 5, tzinfo=timezone.utc)

        extracted = ExtractedContent(
            content='Document text.',
            source=str(test_file),
            content_type='pdf',
            metadata={'creation_date': pdf_creation},
            document_date=None,  # PDFs don't set mtime
        )

        with (
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
            api._ingestion._file_processor = MagicMock()
            api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

            await api.ingest_from_file(test_file)

            _, kwargs = mock_ingest.call_args
            assert kwargs['event_date'] == pdf_creation

    @pytest.mark.asyncio
    async def test_falls_back_to_now_when_all_sources_none(self, api, tmp_path):
        """When all date sources return None, use datetime.now(UTC)."""
        test_file = tmp_path / 'mystery.txt'
        test_file.write_bytes(b'no dates here')

        extracted = ExtractedContent(
            content='No dates here.',
            source=str(test_file),
            content_type='txt',
            metadata={},
            document_date=None,
        )

        with (
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
            api._ingestion._file_processor = MagicMock()
            api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

            before = datetime.now(timezone.utc)
            await api.ingest_from_file(test_file)
            after = datetime.now(timezone.utc)

            _, kwargs = mock_ingest.call_args
            assert before <= kwargs['event_date'] <= after

    @pytest.mark.asyncio
    async def test_llm_date_wins_over_pdf_creation_date(self, api, tmp_path):
        """LLM date should take priority over PDF metadata creation_date."""
        test_file = tmp_path / 'report.pdf'
        test_file.write_bytes(b'fake pdf')

        pdf_creation = datetime(2024, 1, 1, tzinfo=timezone.utc)
        llm_date = datetime(2019, 7, 15, tzinfo=timezone.utc)

        extracted = ExtractedContent(
            content='This study was conducted in July 2019.',
            source=str(test_file),
            content_type='pdf',
            metadata={'creation_date': pdf_creation},
            document_date=None,
        )

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
            assert kwargs['event_date'] == llm_date
