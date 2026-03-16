import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from memex_core.api import NoteInput
from memex_core.services.ingestion import IngestionService
from memex_core.processing.titles import _is_meaningful_name


@pytest.mark.asyncio
async def test_ingest_from_file_markdown(api, tmp_path):
    # Setup a dummy .md file
    md_file = tmp_path / 'test.md'
    md_file.write_text('# Test Content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        resolved_vault = uuid4()
        mock_resolve.return_value = resolved_vault
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(md_file)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(md_file)
        mock_ingest.assert_called_once_with(mock_note, vault_id=resolved_vault)


@pytest.mark.asyncio
async def test_ingest_from_file_directory(api, tmp_path):
    # Setup a dummy directory
    note_dir = tmp_path / 'my_note'
    note_dir.mkdir()
    (note_dir / 'NOTE.md').write_text('# NoteInput')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        resolved_vault = uuid4()
        mock_resolve.return_value = resolved_vault
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(note_dir)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(note_dir)
        mock_ingest.assert_called_once_with(mock_note, vault_id=resolved_vault)


@pytest.mark.asyncio
async def test_ingest_from_file_markitdown(api, tmp_path):
    # Setup a dummy .docx file
    docx_file = tmp_path / 'test.docx'
    docx_file.write_text('dummy binary content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        # Mock _file_processor.extract on the ingestion service
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = 'Extracted Text'
        extracted.content_type = 'docx'
        extracted.source = str(docx_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {}
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with patch(
            'memex_core.services.ingestion.extract_document_date', new_callable=AsyncMock
        ) as mock_date:
            mock_date.return_value = None
            mock_ingest.return_value = {'status': 'success'}
            result = await api.ingest_from_file(docx_file)

        assert result['status'] == 'success'
        mock_from_file.assert_not_called()
        api._ingestion._file_processor.extract.assert_called_once_with(docx_file)
        assert mock_ingest.call_count == 1
        # The note name should be the file stem
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'test'
        assert called_note.source_uri == str(docx_file.absolute())


@pytest.mark.asyncio
async def test_ingest_from_file_llm_title_when_no_metadata_title(api, tmp_path):
    """When PDF metadata has no title and the file stem is not meaningful,
    LLM title extraction should be used instead of falling through to H1 regex."""
    # Use a temp-file-like name so _is_meaningful_name returns False
    pdf_file = tmp_path / 'tmpabcdef12.pdf'
    pdf_file.write_bytes(b'dummy pdf content')

    assert not _is_meaningful_name('tmpabcdef12')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as _mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        # Mock _file_processor.extract — no metadata title
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = '# Add a Sub Title\n\nThis is a guide about Python testing.'
        extracted.content_type = 'pdf'
        extracted.source = str(pdf_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {}  # No 'title' key — simulates empty PDF metadata
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with (
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
            ) as mock_date,
            patch(
                'memex_core.services.ingestion.extract_title_via_llm',
                new_callable=AsyncMock,
            ) as mock_llm_title,
        ):
            mock_date.return_value = None
            mock_llm_title.return_value = 'Python Testing Guide'
            mock_ingest.return_value = {'status': 'success'}

            result = await api.ingest_from_file(pdf_file)

        assert result['status'] == 'success'
        # LLM title extraction should have been called
        mock_llm_title.assert_called_once()
        # The note should use the LLM-extracted title, not H1 or file stem
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'Python Testing Guide'


@pytest.mark.asyncio
async def test_ingest_from_file_skips_llm_title_when_metadata_title_present(api, tmp_path):
    """When PDF metadata provides a meaningful title, LLM title extraction is skipped."""
    pdf_file = tmp_path / 'tmpabcdef12.pdf'
    pdf_file.write_bytes(b'dummy pdf content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as _mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = '# Add a Sub Title\n\nSome content.'
        extracted.content_type = 'pdf'
        extracted.source = str(pdf_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {'title': 'Real PDF Title'}  # Metadata has a title
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with (
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
            ) as mock_date,
            patch(
                'memex_core.services.ingestion.extract_title_via_llm',
                new_callable=AsyncMock,
            ) as mock_llm_title,
        ):
            mock_date.return_value = None
            mock_ingest.return_value = {'status': 'success'}

            result = await api.ingest_from_file(pdf_file)

        assert result['status'] == 'success'
        # LLM title extraction should NOT have been called — metadata title is meaningful
        mock_llm_title.assert_not_called()
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'Real PDF Title'
