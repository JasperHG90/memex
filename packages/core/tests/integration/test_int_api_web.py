import pytest
from unittest.mock import patch


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_from_url(api, metastore, fake_retain_factory):
    # Mock MemoryEngine.retain to return success and persist to DB
    api.memory.retain.side_effect = fake_retain_factory

    # Mock WebContentProcessor
    with patch('memex_core.services.ingestion.WebContentProcessor.fetch_and_extract') as mock_fetch:
        from memex_core.processing.models import ExtractedContent

        mock_fetch.return_value = ExtractedContent(
            content='This is a test article about Python.',
            source='https://python.org/test',
            content_type='web',
            metadata={
                'title': 'Python Testing',
                'date': '2023-10-27',
                'author': 'Tester',
                'hostname': 'python.org',
            },
        )

        # Execute
        result = await api.ingest_from_url('https://python.org/test')

        assert result['status'] == 'success'
        assert result['note_id'] is not None

        # Verify Metastore
        async with metastore.session() as session:
            from memex_core.memory.sql_models import Note

            doc = await session.get(Note, result['note_id'])
            assert doc is not None
            assert doc.original_text.startswith('---')
            assert 'This is a test article about Python.' in doc.original_text
