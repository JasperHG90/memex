import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from memex_core.api import NoteInput
from memex_core.services.ingestion import IngestionService


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
        mock_resolve.return_value = uuid4()
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(md_file)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(md_file)
        mock_ingest.assert_called_once_with(mock_note)


@pytest.mark.asyncio
async def test_ingest_from_file_directory(api, tmp_path):
    # Setup a dummy directory
    note_dir = tmp_path / 'my_note'
    note_dir.mkdir()
    (note_dir / 'NOTE.md').write_text('# NoteInput')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
    ):
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(note_dir)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(note_dir)
        mock_ingest.assert_called_once_with(mock_note)


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
