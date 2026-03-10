import pytest
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from memex_core.services.ingestion import IngestionService


@pytest.mark.asyncio
async def test_uuid_idempotency_same_file(
    mock_metastore, mock_filestore, mock_config, api, tmp_path, mock_session
):
    """
    Verify that the same source file results in the same UUID across
    multiple ingestions (stable identity via source_uri).
    """
    pdf_file = tmp_path / 'test.pdf'
    pdf_file.write_text('Dummy content')

    # Mock vault resolution
    mock_vault = MagicMock()
    mock_vault.id = uuid4()
    mock_vault.name = 'global'
    mock_session.exec.return_value.all.return_value = [mock_vault]

    with patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest:
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = 'This is the raw content'
        extracted.content_type = 'pdf'
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {}
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with patch(
            'memex_core.services.ingestion.extract_document_date', new_callable=AsyncMock
        ) as mock_date:
            mock_date.return_value = None
            mock_ingest.return_value = {'status': 'success'}
            await api.ingest_from_file(pdf_file)
            await api.ingest_from_file(pdf_file)

        assert mock_ingest.call_count == 2
        note1 = mock_ingest.call_args_list[0][0][0]
        note2 = mock_ingest.call_args_list[1][0][0]

        # UUID is the same across ingestions
        assert note1.uuid == note2.uuid

        # Verify the UUID format is what we expect (MD5 of source_uri = note_key)
        expected_uuid = hashlib.md5(str(pdf_file.absolute()).encode('utf-8')).hexdigest()
        assert note1.uuid == expected_uuid
