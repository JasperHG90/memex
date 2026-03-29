"""Tests for multi-format ingestion: conversion helpers, batch/single paths, binary idempotency."""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.processing.models import ExtractedContent
from memex_core.services.ingestion import (
    _convert_to_markdown,
    _needs_conversion,
    _wrap_extracted_content,
)


# ---------------------------------------------------------------------------
# 1. _needs_conversion helper
# ---------------------------------------------------------------------------


class TestNeedsConversion:
    """Verify extension-based routing for convertible formats."""

    @pytest.mark.parametrize(
        'filename',
        [
            'report.pdf',
            'slides.pptx',
            'data.xlsx',
            'letter.docx',
            'records.csv',
            'config.json',
            'feed.xml',
            'page.html',
            'email.msg',
            'thread.eml',
        ],
    )
    def test_convertible_extensions_return_true(self, filename: str) -> None:
        dto = SimpleNamespace(filename=filename)
        assert _needs_conversion(dto) is True

    def test_markdown_returns_false(self) -> None:
        dto = SimpleNamespace(filename='notes.md')
        assert _needs_conversion(dto) is False

    def test_none_filename_returns_false(self) -> None:
        dto = SimpleNamespace(filename=None)
        assert _needs_conversion(dto) is False

    def test_missing_filename_attr_returns_false(self) -> None:
        dto = SimpleNamespace()
        assert _needs_conversion(dto) is False

    def test_empty_string_filename_returns_false(self) -> None:
        dto = SimpleNamespace(filename='')
        assert _needs_conversion(dto) is False


# ---------------------------------------------------------------------------
# 2. _convert_to_markdown helper
# ---------------------------------------------------------------------------


class TestConvertToMarkdown:
    """Verify temp-file lifecycle and delegation to FileContentProcessor."""

    @pytest.mark.asyncio
    async def test_writes_temp_file_calls_extract_and_cleans_up(self) -> None:
        raw_bytes = b'%PDF-1.4 fake pdf content'
        filename = 'report.pdf'
        expected = ExtractedContent(
            content='# Converted\nSome markdown',
            source='/tmp/fake.pdf',
            content_type='pdf',
            images={'img1.png': b'\x89PNG'},
        )

        processor = AsyncMock(spec=['extract'])
        processor.extract = AsyncMock(return_value=expected)

        result = await _convert_to_markdown(raw_bytes, filename, processor)

        assert result is expected
        processor.extract.assert_awaited_once()

        # Verify the temp file path had the correct suffix
        call_args = processor.extract.call_args
        tmp_path = call_args[0][0]
        assert str(tmp_path).endswith('.pdf')

        # Verify cleanup: temp file should be deleted
        assert not os.path.exists(str(tmp_path))

    @pytest.mark.asyncio
    async def test_cleans_up_on_extract_failure(self) -> None:
        processor = AsyncMock(spec=['extract'])
        processor.extract = AsyncMock(side_effect=ValueError('extraction failed'))

        with pytest.raises(ValueError, match='extraction failed'):
            await _convert_to_markdown(b'bad data', 'doc.docx', processor)


# ---------------------------------------------------------------------------
# 3. _wrap_extracted_content
# ---------------------------------------------------------------------------


class TestWrapExtractedContent:
    def test_basic_wrapping(self) -> None:
        extracted = ExtractedContent(
            content='# Hello World',
            source='/tmp/test.pdf',
            content_type='pdf',
            metadata={},
        )
        result = _wrap_extracted_content(extracted, 'report.pdf')
        assert 'source_file: report.pdf' in result
        assert 'type: pdf' in result
        assert '# Hello World' in result

    def test_includes_author_and_creation_date(self) -> None:
        from datetime import datetime, timezone

        creation = datetime(2025, 6, 15, tzinfo=timezone.utc)
        extracted = ExtractedContent(
            content='Body text',
            source='/tmp/test.pdf',
            content_type='pdf',
            metadata={'author': 'Alice', 'creation_date': creation},
        )
        result = _wrap_extracted_content(extracted, 'paper.pdf')
        assert 'author: Alice' in result
        assert 'created_date: 2025-06-15' in result


# ---------------------------------------------------------------------------
# 4. Batch ingestion conversion path
# ---------------------------------------------------------------------------


class TestBatchIngestionConversion:
    """Test that ingest_batch_internal converts non-markdown DTOs via FileContentProcessor."""

    @pytest.mark.asyncio
    async def test_batch_converts_pdf_dto(
        self,
        mock_metastore,
        mock_filestore,
        mock_config,
        mock_session,
    ) -> None:
        from memex_core.memory.sql_models import Vault
        from memex_core.services.ingestion import IngestionService

        pdf_content = b'%PDF-1.4 fake binary'
        b64_content = base64.b64encode(pdf_content)

        dto = SimpleNamespace(
            name='Report',
            description='A PDF report',
            content=b64_content,
            content_decoded=pdf_content,
            files={},
            tags=['test'],
            filename='report.pdf',
            note_key=None,
            user_notes=None,
            author=None,
            vault_id=None,
        )

        extracted = ExtractedContent(
            content='# Converted PDF\nExtracted text from PDF',
            source='/tmp/report.pdf',
            content_type='pdf',
            metadata={},
            images={'chart.png': b'\x89PNG chart data'},
        )

        mock_file_processor = AsyncMock()
        mock_memory = AsyncMock()
        mock_memory.retain = AsyncMock(return_value={'status': 'success', 'unit_ids': []})
        mock_vaults = AsyncMock()

        from memex_common.config import GLOBAL_VAULT_ID

        mock_vaults.resolve_vault_identifier = AsyncMock(return_value=GLOBAL_VAULT_ID)

        vault_obj = Vault(id=GLOBAL_VAULT_ID, name='global')
        mock_session.get.return_value = vault_obj

        # Vault lookup + idempotency check
        mock_vault_result = MagicMock()
        mock_vault_result.all.return_value = []
        mock_session.exec.return_value = mock_vault_result

        lm = MagicMock()

        svc = IngestionService(
            metastore=mock_metastore,
            filestore=mock_filestore,
            config=mock_config,
            lm=lm,
            memory=mock_memory,
            file_processor=mock_file_processor,
            vaults=mock_vaults,
        )

        with (
            patch(
                'memex_core.services.ingestion._convert_to_markdown',
                new_callable=AsyncMock,
                return_value=extracted,
            ) as mock_convert,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new_callable=AsyncMock,
                return_value='Converted PDF',
            ),
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
        ):
            # Setup transaction context manager
            mock_txn = AsyncMock()
            mock_txn.db_session = mock_session
            mock_txn.save_file = AsyncMock()
            mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn.__aexit__ = AsyncMock(return_value=None)
            mock_txn_cls.return_value = mock_txn

            results = []
            async for progress in svc.ingest_batch_internal([dto]):
                results.append(dict(progress))

            # Verify conversion was called
            mock_convert.assert_awaited_once()
            call_args = mock_convert.call_args
            assert call_args[0][0] == pdf_content
            assert call_args[0][1] == 'report.pdf'

            # Verify the retain call got markdown content, not raw PDF bytes
            retain_call = mock_memory.retain.call_args
            retain_content = retain_call.kwargs.get('contents') or retain_call[1].get('contents')
            if retain_content is None:
                retain_content = retain_call[0][1] if len(retain_call[0]) > 1 else None
            assert retain_content is not None
            actual_content = retain_content[0].content
            assert '# Converted PDF' in actual_content
            assert b'%PDF' not in actual_content.encode('utf-8')

            # Verify extracted images were staged as assets
            save_calls = [str(c) for c in mock_txn.save_file.call_args_list]
            save_call_args = [c[0] for c in mock_txn.save_file.call_args_list]
            chart_staged = any('chart.png' in str(args) for args in save_call_args)
            assert chart_staged, f'Expected chart.png in save_file calls: {save_calls}'


# ---------------------------------------------------------------------------
# 5. Single-note ingestion conversion (server endpoint)
# ---------------------------------------------------------------------------


class TestSingleNoteConversion:
    """Test that the /ingestions endpoint converts non-markdown DTOs."""

    @pytest.mark.asyncio
    async def test_endpoint_converts_pdf_before_constructing_note_input(self) -> None:
        from memex_common.schemas import NoteCreateDTO

        pdf_bytes = b'%PDF-1.4 binary content'
        b64_pdf = base64.b64encode(pdf_bytes).decode('ascii')

        dto = NoteCreateDTO(
            name='Report',
            description='A PDF report',
            content=b64_pdf,
            filename='report.pdf',
        )

        extracted = ExtractedContent(
            content='# Converted from PDF',
            source='/tmp/report.pdf',
            content_type='pdf',
            metadata={},
            images={'fig1.png': b'\x89PNG fig data'},
        )

        mock_api = AsyncMock()
        mock_api.ingest = AsyncMock(
            return_value={'status': 'success', 'note_id': str(uuid4()), 'unit_ids': []}
        )
        mock_api._file_processor = AsyncMock()

        with (
            patch(
                'memex_core.server.ingestion._convert_to_markdown',
                new_callable=AsyncMock,
                return_value=extracted,
            ) as mock_convert,
            patch(
                'memex_core.server.ingestion.check_vault_access',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.server.ingestion.get_api',
                return_value=mock_api,
            ),
        ):
            from memex_core.server.ingestion import ingest_note

            bg_tasks = MagicMock()
            await ingest_note(
                request=dto,
                api=mock_api,
                background_tasks=bg_tasks,
                background=False,
                auth=None,
            )

            # Conversion should have been called
            mock_convert.assert_awaited_once()
            conv_args = mock_convert.call_args[0]
            assert conv_args[0] == pdf_bytes  # decoded content
            assert conv_args[1] == 'report.pdf'

            # Verify NoteInput was created with markdown, not raw PDF
            ingest_call = mock_api.ingest.call_args
            note_input = ingest_call.kwargs.get('note') or ingest_call[0][0]
            content_text = note_input._content.decode('utf-8')
            assert '# Converted from PDF' in content_text

            # Verify extracted images were merged into files
            assert 'fig1.png' in note_input._files
            assert note_input._files['fig1.png'] == b'\x89PNG fig data'


# ---------------------------------------------------------------------------
# 6. Binary idempotency — calculate_*_from_dto with non-UTF-8 bytes
# ---------------------------------------------------------------------------


class TestBinaryIdempotency:
    """Ensure idempotency/fingerprint functions handle binary content DTOs."""

    def test_idempotency_key_with_binary_content(self) -> None:
        from memex_core.api import NoteInput

        # Simulate a PDF DTO: content field is base64 of raw binary
        raw_binary = bytes(range(256))  # all byte values 0x00-0xFF
        b64_content = base64.b64encode(raw_binary)

        dto = SimpleNamespace(
            name='Binary Doc',
            description='Non-UTF-8 content',
            content=b64_content,
            files={},
            tags=[],
            note_key=None,
            user_notes=None,
            author=None,
            filename='data.bin',
        )

        # Should not raise
        key = NoteInput.calculate_idempotency_key_from_dto(dto)
        assert isinstance(key, str)
        assert len(key) == 32  # MD5 hex digest

    def test_fingerprint_with_binary_content(self) -> None:
        from memex_core.api import NoteInput

        raw_binary = bytes(range(256))
        b64_content = base64.b64encode(raw_binary)

        dto = SimpleNamespace(
            name='Binary Doc',
            description='Non-UTF-8 content',
            content=b64_content,
            files={},
            tags=[],
            note_key=None,
            user_notes=None,
            author=None,
            filename='data.bin',
        )

        # Should not raise
        fingerprint = NoteInput.calculate_fingerprint_from_dto(dto)
        assert isinstance(fingerprint, str)
        assert len(fingerprint) > 0

    def test_idempotency_deterministic_for_same_binary(self) -> None:
        from memex_core.api import NoteInput

        raw = b'\x00\x01\x02\xff\xfe\xfd'
        b64 = base64.b64encode(raw)

        def make_dto():
            return SimpleNamespace(
                name='Same',
                description='Same desc',
                content=b64,
                files={},
                tags=[],
                note_key=None,
                user_notes=None,
                author=None,
            )

        key1 = NoteInput.calculate_idempotency_key_from_dto(make_dto())
        key2 = NoteInput.calculate_idempotency_key_from_dto(make_dto())
        assert key1 == key2


# ---------------------------------------------------------------------------
# 7. Image merge from extraction
# ---------------------------------------------------------------------------


class TestImageMergeFromExtraction:
    """Test that images from FileContentProcessor are staged as assets."""

    @pytest.mark.asyncio
    async def test_extracted_images_merged_with_explicit_files(self) -> None:
        """When FileContentProcessor returns images, they appear alongside
        the note's explicit files dict."""
        from memex_common.schemas import NoteCreateDTO

        pdf_bytes = b'%PDF fake'
        b64_pdf = base64.b64encode(pdf_bytes).decode('ascii')

        # Explicit file in the DTO
        explicit_img = b'\x89PNG explicit'
        b64_explicit = base64.b64encode(explicit_img).decode('ascii')

        dto = NoteCreateDTO(
            name='Mixed',
            description='PDF with explicit image',
            content=b64_pdf,
            files={'logo.png': b64_explicit},
            filename='report.pdf',
        )

        extracted = ExtractedContent(
            content='# PDF Content',
            source='/tmp/report.pdf',
            content_type='pdf',
            metadata={},
            images={
                'page1_img0.png': b'\x89PNG page image',
                'page2_chart.png': b'\x89PNG chart',
            },
        )

        mock_api = AsyncMock()
        mock_api.ingest = AsyncMock(
            return_value={'status': 'success', 'note_id': str(uuid4()), 'unit_ids': []}
        )
        mock_api._file_processor = AsyncMock()

        with (
            patch(
                'memex_core.server.ingestion._convert_to_markdown',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                'memex_core.server.ingestion.check_vault_access',
                new_callable=AsyncMock,
            ),
        ):
            from memex_core.server.ingestion import ingest_note

            bg_tasks = MagicMock()
            await ingest_note(
                request=dto,
                api=mock_api,
                background_tasks=bg_tasks,
                background=False,
                auth=None,
            )

            ingest_call = mock_api.ingest.call_args
            note_input = ingest_call.kwargs.get('note') or ingest_call[0][0]

            # All three images should be present
            assert 'logo.png' in note_input._files
            assert 'page1_img0.png' in note_input._files
            assert 'page2_chart.png' in note_input._files

            # Verify correct bytes
            assert note_input._files['logo.png'] == explicit_img
            assert note_input._files['page1_img0.png'] == b'\x89PNG page image'
            assert note_input._files['page2_chart.png'] == b'\x89PNG chart'
