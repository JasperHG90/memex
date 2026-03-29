"""Integration tests for multi-format ingestion via NoteCreateDTO batch and single paths."""

import base64
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import fitz  # type: ignore
import pytest

from memex_common.schemas import NoteCreateDTO
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


def _create_pdf_with_image(path, *, body_text: str = 'Document with image.') -> None:
    """Create a synthetic PDF that contains an embedded image."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), body_text)
    # Insert a tiny 2x2 red PNG as an embedded image
    import struct
    import zlib

    def _make_tiny_png() -> bytes:
        width, height = 2, 2
        raw_data = b''
        for _ in range(height):
            raw_data += b'\x00'  # filter byte
            for _ in range(width):
                raw_data += b'\xff\x00\x00'  # red pixel (RGB)
        compressed = zlib.compress(raw_data)

        def _chunk(chunk_type: bytes, data: bytes) -> bytes:
            c = chunk_type + data
            crc = zlib.crc32(c) & 0xFFFFFFFF
            return struct.pack('>I', len(data)) + c + struct.pack('>I', crc)

        ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        png = b'\x89PNG\r\n\x1a\n'
        png += _chunk(b'IHDR', ihdr_data)
        png += _chunk(b'IDAT', compressed)
        png += _chunk(b'IEND', b'')
        return png

    png_bytes = _make_tiny_png()
    rect = fitz.Rect(72, 100, 172, 200)
    page.insert_image(rect, stream=png_bytes)
    doc.save(str(path))
    doc.close()


def _make_pdf_dto(
    pdf_path,
    *,
    name: str = 'Test PDF',
    description: str = 'A test PDF document',
    filename: str = 'test.pdf',
    tags: list[str] | None = None,
) -> NoteCreateDTO:
    """Read a PDF file and wrap it in a NoteCreateDTO with base64-encoded content."""
    pdf_bytes = pdf_path.read_bytes()
    b64_content = base64.b64encode(pdf_bytes)
    return NoteCreateDTO(
        name=name,
        description=description,
        content=b64_content,
        filename=filename,
        tags=tags or [],
    )


def _make_md_dto(
    *,
    name: str = 'Test Markdown',
    description: str = 'A test markdown note',
    content_text: str | None = None,
) -> NoteCreateDTO:
    """Create a NoteCreateDTO with plain markdown content."""
    text = content_text or f'# Hello World\n\nUnique content {uuid4()}'
    b64_content = base64.b64encode(text.encode('utf-8'))
    return NoteCreateDTO(
        name=name,
        description=description,
        content=b64_content,
        tags=[],
    )


async def _collect_batch_results(api, dtos, vault_id=None):
    """Iterate through ingest_batch_internal and return the final progress dict."""
    final = None
    async for progress in api.ingest_batch_internal(dtos, vault_id=vault_id):
        final = progress
    return final


class TestMultiFormatBatchIngestion:
    """Integration tests for multi-format ingestion via batch DTO path."""

    @pytest.mark.asyncio
    async def test_batch_ingest_pdf_via_dto(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """A PDF NoteCreateDTO should be converted to markdown with proper frontmatter."""
        unique = uuid4()
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Integration Report',
            author='Test Author',
            creation_date='D:20250310120000Z',
            body_text=f'Important findings about integration testing. Unique {unique}',
        )

        dto = _make_pdf_dto(pdf_path, filename='test.pdf')

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Integration Report',
        ):
            results = await _collect_batch_results(api, [dto])

        assert results is not None
        assert results['processed_count'] == 1

        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]

        # Content should be markdown, not raw PDF bytes
        assert 'source_file: test.pdf' in retain_content.content
        assert 'type: pdf' in retain_content.content
        assert 'integration testing' in retain_content.content.lower()

    @pytest.mark.asyncio
    async def test_batch_ingest_markdown_unchanged(
        self, api, metastore, memex_config, fake_retain_factory
    ):
        """A plain markdown DTO (no filename) should pass through content unchanged."""
        unique_text = f'# My Note\n\nOriginal markdown content {uuid4()}'
        dto = _make_md_dto(content_text=unique_text)

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='My Note',
        ):
            results = await _collect_batch_results(api, [dto])

        assert results is not None
        assert results['processed_count'] == 1

        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]

        # Markdown content should be passed through as-is (no frontmatter wrapping)
        assert '# My Note' in retain_content.content
        assert 'Original markdown content' in retain_content.content
        # Should NOT have source_file frontmatter (it's not a converted file)
        assert 'source_file:' not in retain_content.content

    @pytest.mark.asyncio
    async def test_batch_ingest_mixed_formats(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """A batch with both .md and .pdf DTOs should handle each format correctly."""
        # Create markdown DTO
        md_text = f'# Markdown Note\n\nPlain text content {uuid4()}'
        md_dto = _make_md_dto(
            name='Markdown Note',
            description='A markdown note',
            content_text=md_text,
        )

        # Create PDF DTO
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='PDF Note',
            body_text=f'PDF body content {uuid4()}',
        )
        pdf_dto = _make_pdf_dto(
            pdf_path,
            name='PDF Note',
            description='A PDF note',
            filename='mixed_test.pdf',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            side_effect=lambda content, name, lm: name or 'Untitled',
        ):
            results = await _collect_batch_results(api, [md_dto, pdf_dto])

        assert results is not None
        assert results['processed_count'] == 2

        # Verify both retain calls
        assert mock_memory.retain.call_count == 2

        # First call: markdown — should not have source_file frontmatter
        md_call = mock_memory.retain.call_args_list[0][1]
        md_content = md_call['contents'][0].content
        assert '# Markdown Note' in md_content
        assert 'source_file:' not in md_content

        # Second call: PDF — should have conversion frontmatter
        pdf_call = mock_memory.retain.call_args_list[1][1]
        pdf_content = pdf_call['contents'][0].content
        assert 'source_file: mixed_test.pdf' in pdf_content
        assert 'type: pdf' in pdf_content


class TestMultiFormatSingleIngestion:
    """Integration tests for multi-format ingestion via the single-note path."""

    @pytest.mark.asyncio
    async def test_single_note_ingest_pdf(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """A single NoteInput constructed from a PDF DTO should convert to markdown."""
        unique = uuid4()
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Single Report',
            author='Jane Doe',
            creation_date='D:20250201080000Z',
            body_text=f'Report content for single ingestion. Unique {unique}',
        )

        # Use ingest_from_file which handles PDF conversion internally
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

        # Content should be converted markdown with frontmatter
        assert 'source_file:' in retain_content.content
        assert 'type: pdf' in retain_content.content
        assert 'single ingestion' in retain_content.content.lower()
        assert 'author: Jane Doe' in retain_content.content


class TestPdfImageExtraction:
    """Integration tests for PDF image extraction as assets."""

    @pytest.mark.asyncio
    async def test_pdf_images_extracted_as_assets(
        self, api, metastore, filestore, memex_config, fake_retain_factory, tmp_path
    ):
        """Images embedded in a PDF should be extracted and stored as assets."""
        unique = uuid4()
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_pdf_with_image(
            pdf_path,
            body_text=f'Document with embedded image. Unique {unique}',
        )

        dto = _make_pdf_dto(pdf_path, filename='images_test.pdf')

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Image Document',
        ):
            results = await _collect_batch_results(api, [dto])

        assert results is not None
        assert results['processed_count'] == 1

        # Verify that assets were staged (images from PDF extraction)
        call_kwargs = mock_memory.retain.call_args[1]
        retain_content = call_kwargs['contents'][0]
        asset_list = retain_content.payload.get('assets', [])
        # The PDF with an embedded image should produce at least one asset
        assert len(asset_list) > 0, 'Expected extracted images to be stored as assets'


class TestIdempotencyWithBinaryContent:
    """Integration tests for idempotency checks with binary (PDF) content."""

    @pytest.mark.asyncio
    async def test_idempotency_with_binary_content(
        self, api, metastore, memex_config, fake_retain_factory, tmp_path
    ):
        """Sending the same PDF DTO twice should skip the second ingestion."""
        unique = uuid4()
        pdf_path = tmp_path / f'tmp_{uuid4().hex[:8]}.pdf'
        _create_synthetic_pdf(
            pdf_path,
            title='Idempotency Test',
            body_text=f'Content for idempotency test. Unique {unique}',
        )

        dto = _make_pdf_dto(
            pdf_path,
            name='Idempotency Test',
            filename='idempotent.pdf',
        )

        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(side_effect=fake_retain_factory)
        api._ingestion.memory = mock_memory
        api._ingestion._file_processor = FileContentProcessor()

        # First ingestion — should succeed
        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Idempotency Test',
        ):
            first_results = await _collect_batch_results(api, [dto])

        assert first_results is not None
        assert first_results['processed_count'] == 1
        assert first_results['skipped_count'] == 0

        # Second ingestion with the same DTO — should be skipped
        with patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Idempotency Test',
        ):
            second_results = await _collect_batch_results(api, [dto])

        assert second_results is not None
        assert second_results['skipped_count'] == 1
        assert second_results['processed_count'] == 0
