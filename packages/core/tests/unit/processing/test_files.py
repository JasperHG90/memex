from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch
from memex_core.processing.files import FileContentProcessor, _file_mtime_utc, _parse_pdf_date


@pytest.fixture
def file_processor():
    return FileContentProcessor()


@pytest.mark.asyncio
async def test_extract_pdf_mock(file_processor):
    # Setup mock for pymupdf4llm
    mock_stat = MagicMock()
    mock_stat.st_mtime = 1672531200.0  # 2023-01-01 00:00:00 UTC

    mock_doc = MagicMock()
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_doc.metadata = {
        'title': 'Test PDF Title',
        'author': 'Test Author',
        'creationDate': "D:20230101000000Z00'00'",
    }

    with patch(
        'memex_core.processing.files.pymupdf4llm.to_markdown',
        return_value='Extracted PDF Content',
    ) as mock_to_markdown:
        with patch('memex_core.processing.files.fitz.open', return_value=mock_doc):
            with patch('memex_core.processing.files.Path.exists', return_value=True):
                with patch('memex_core.processing.files.Path.stat', return_value=mock_stat):
                    with patch('pathlib.Path.glob') as mock_glob:
                        mock_img = MagicMock()
                        mock_img.is_file.return_value = True
                        mock_img.name = 'image1.png'
                        mock_img.read_bytes.return_value = b'fake_image_data'
                        mock_glob.return_value = [mock_img]

                        result = await file_processor.extract('test.pdf')

                        assert result.content == 'Extracted PDF Content'
                        assert result.source == 'test.pdf'
                        assert result.content_type == 'pdf'
                        assert result.images['image1.png'] == b'fake_image_data'
                        # document_date should be None for PDFs
                        assert result.document_date is None
                        # metadata should contain PDF info
                        assert result.metadata['title'] == 'Test PDF Title'
                        assert result.metadata['author'] == 'Test Author'
                        assert result.metadata['creation_date'] == datetime(
                            2023, 1, 1, tzinfo=timezone.utc
                        )
                        assert result.metadata['file_mtime'] is not None

                        mock_to_markdown.assert_called_once()
                        args, kwargs = mock_to_markdown.call_args
                        assert args[0] == 'test.pdf'
                        assert kwargs['force_text'] is True
                        assert kwargs['write_images'] is True


@pytest.mark.asyncio
async def test_extract_pdf_failure(file_processor):
    with patch('memex_core.processing.files.Path.exists', return_value=True):
        with patch(
            'memex_core.processing.files.pymupdf4llm.to_markdown',
            side_effect=RuntimeError('PDF error'),
        ):
            with pytest.raises(ValueError, match='PDF extraction failed'):
                await file_processor.extract('bad.pdf')


@pytest.mark.asyncio
async def test_extract_file_not_found(file_processor):
    with pytest.raises(FileNotFoundError):
        await file_processor.extract('non_existent.docx')


@pytest.mark.asyncio
async def test_extract_markitdown_failure(file_processor):
    # Test failure for non-PDF file
    with patch('memex_core.processing.files.Path.exists', return_value=True):
        with patch(
            'memex_core.processing.files.MarkItDown.convert',
            side_effect=RuntimeError('Conversion error'),
        ):
            with pytest.raises(ValueError, match='Extraction failed'):
                await file_processor.extract('corrupt.pptx')


@pytest.mark.asyncio
async def test_extract_json(file_processor):
    # MarkItDown handles JSON too
    mock_doc = MagicMock()
    mock_doc.text_content = '{"key": "value"}'

    mock_stat = MagicMock()
    mock_stat.st_mtime = 1700000000.0  # 2023-11-14

    with patch('memex_core.processing.files.MarkItDown.convert', return_value=mock_doc):
        with patch('memex_core.processing.files.Path.exists', return_value=True):
            with patch('memex_core.processing.files.Path.stat', return_value=mock_stat):
                result = await file_processor.extract('data.json')
                assert result.content == '{"key": "value"}'
                assert result.content_type == 'json'
                # document_date should be set from file mtime
                assert result.document_date is not None
                assert result.document_date.tzinfo is not None


def test_file_mtime_utc_valid(tmp_path):
    """_file_mtime_utc returns a timezone-aware datetime for real files."""
    test_file = tmp_path / 'test.txt'
    test_file.write_text('hello')
    result = _file_mtime_utc(test_file)
    assert result is not None
    assert result.tzinfo is not None


def test_file_mtime_utc_nonexistent():
    """_file_mtime_utc returns None for non-existent files."""
    from pathlib import Path

    result = _file_mtime_utc(Path('/nonexistent/path/file.txt'))
    assert result is None


# ---------------------------------------------------------------------------
# _parse_pdf_date
# ---------------------------------------------------------------------------


class TestParsePdfDate:
    def test_valid_date_with_prefix(self):
        result = _parse_pdf_date("D:20260310064822Z00'00'")
        assert result == datetime(2026, 3, 10, 6, 48, 22, tzinfo=timezone.utc)

    def test_valid_date_without_prefix(self):
        result = _parse_pdf_date('20230101120000')
        assert result == datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_none_input(self):
        assert _parse_pdf_date(None) is None

    def test_empty_string(self):
        assert _parse_pdf_date('') is None

    def test_invalid_format(self):
        assert _parse_pdf_date('not-a-date') is None

    def test_short_string(self):
        assert _parse_pdf_date('D:2023') is None


@pytest.mark.asyncio
async def test_extract_pdf_no_fitz_metadata(file_processor):
    """PDF extraction works even if fitz metadata reading fails."""
    mock_stat = MagicMock()
    mock_stat.st_mtime = 1672531200.0

    with patch(
        'memex_core.processing.files.pymupdf4llm.to_markdown',
        return_value='Content',
    ):
        with patch(
            'memex_core.processing.files.fitz.open',
            side_effect=Exception('fitz error'),
        ):
            with patch('memex_core.processing.files.Path.exists', return_value=True):
                with patch('memex_core.processing.files.Path.stat', return_value=mock_stat):
                    with patch('pathlib.Path.glob', return_value=[]):
                        result = await file_processor.extract('test.pdf')
                        assert result.document_date is None
                        assert result.metadata.get('file_mtime') is not None
