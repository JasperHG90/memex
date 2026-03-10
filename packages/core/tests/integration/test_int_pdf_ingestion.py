"""Integration tests for PDF ingestion: date priority, title from metadata, frontmatter."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fitz  # type: ignore
import pytest

from memex_core.processing.files import FileContentProcessor

pytestmark = [pytest.mark.integration]


def _create_synthetic_pdf(
    path,
    *,
    title: str = '',
    author: str = '',
    creation_date: str = '',
    body_text: str = 'Sample document content.',
) -> None:
    """Create a synthetic PDF with known metadata using fitz (pymupdf)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), body_text)
    if title or author or creation_date:
        meta = doc.metadata or {}
        if title:
            meta['title'] = title
        if author:
            meta['author'] = author
        if creation_date:
            meta['creationDate'] = creation_date
        doc.set_metadata(meta)
    doc.save(str(path))
    doc.close()


class TestPdfIngestionWithMockedLLM:
    """Integration tests for PDF file ingestion with mocked LLM."""

    @pytest.mark.asyncio
    async def test_pdf_title_from_metadata(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """PDF metadata title should be used as the note name."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='My Research Paper',
            author='Jane Doe',
            creation_date='D:20250115120000Z',
            body_text=f'Unique content {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        # Verify retain was called with PDF title as note_name
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        assert retain_content.payload['note_name'] == 'My Research Paper'

    @pytest.mark.asyncio
    async def test_pdf_author_in_frontmatter(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """PDF metadata author should appear in the ingested note frontmatter."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Test Doc',
            author='John Smith',
            creation_date='D:20250115120000Z',
            body_text=f'Unique content {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        assert 'author: John Smith' in retain_content.content

    @pytest.mark.asyncio
    async def test_pdf_created_date_in_frontmatter(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """PDF metadata creation date should appear in the ingested note frontmatter."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Test Doc',
            author='Author',
            creation_date='D:20250115120000Z',
            body_text=f'Unique content {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        assert 'created_date: 2025-01-15' in retain_content.content

    @pytest.mark.asyncio
    async def test_pdf_date_priority_llm_over_metadata(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """LLM-extracted date should take priority over PDF metadata creation date."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Test Doc',
            creation_date='D:20250115120000Z',
            body_text=f'Published: March 1, 2024. Unique {uuid4()}',
        )

        llm_date = datetime(2024, 3, 1, tzinfo=timezone.utc)
        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=llm_date,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        assert retain_content.event_date == llm_date

    @pytest.mark.asyncio
    async def test_pdf_date_falls_back_to_metadata_when_no_llm(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """When LLM returns None, PDF metadata creation_date should be used."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Test Doc',
            creation_date='D:20250115120000Z',
            body_text=f'No date in content. Unique {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        # Should use PDF metadata creation_date (2025-01-15)
        assert retain_content.event_date.year == 2025
        assert retain_content.event_date.month == 1
        assert retain_content.event_date.day == 15

    @pytest.mark.asyncio
    async def test_pdf_publish_date_not_today(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """Ingesting a PDF with metadata date should NOT result in today's date."""
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Historical Report',
            creation_date='D:20230601080000Z',
            body_text=f'Report from 2023. Unique {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        today = datetime.now(timezone.utc).date()
        assert retain_content.event_date.date() != today

    @pytest.mark.asyncio
    async def test_pdf_title_fallback_to_stem(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """When PDF has no title metadata, file stem should be used as name."""
        pdf_path = tmp_path / f'my_document_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            body_text=f'Content without title metadata. Unique {uuid4()}',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        # Should fall back to file stem
        assert pdf_path.stem in retain_content.payload['note_name']


@pytest.mark.llm
class TestPdfIngestionWithRealLLM:
    """Tests using a real LLM to extract dates from synthetic PDFs."""

    @pytest.mark.asyncio
    async def test_llm_date_extraction_from_pdf_content(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """Real LLM should extract 'Published: January 15, 2025' from PDF content.

        LLM date should take priority over PDF metadata creationDate.
        """
        import dspy

        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='LLM Test Paper',
            author='Test Author',
            # PDF metadata says 2020 — LLM should override with content date
            creation_date='D:20200601000000Z',
            body_text=(
                'Published: January 15, 2025\n\n'
                'This is a research paper about artificial intelligence.\n'
                f'Unique identifier: {uuid4()}'
            ),
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        # Use real LLM for date extraction
        lm = dspy.LM('gemini/gemini-2.0-flash')
        api._ingestion.lm = lm

        result = await api.ingest_from_file(str(pdf_path))

        assert result['status'] == 'success'
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]

        # LLM should extract January 15, 2025 from content
        assert retain_content.event_date.year == 2025
        assert retain_content.event_date.month == 1
        assert retain_content.event_date.day == 15
