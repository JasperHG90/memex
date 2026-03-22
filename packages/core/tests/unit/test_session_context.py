from uuid import uuid4
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from memex_core.context import set_session_id
from memex_core.memory.extraction import storage


@pytest.mark.asyncio
async def test_document_session_id_in_storage():
    """Verify handle_document_tracking persists the current session ID."""
    custom_sid = 'doc-ingest-session'
    set_session_id(custom_sid)

    doc_id = str(uuid4())
    content = 'session test content'
    mock_session = AsyncMock()

    with patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert:
        mock_insert_stmt = MagicMock()
        mock_insert.return_value.values.return_value = mock_insert_stmt
        # on_conflict_do_update is the upsert
        mock_insert_stmt.on_conflict_do_update.return_value = MagicMock()

        await storage.handle_document_tracking(mock_session, doc_id, content, is_first_batch=False)

        # Verify values passed to insert
        mock_insert.return_value.values.assert_called_once()
        args, kwargs = mock_insert.return_value.values.call_args

        # storage.py uses values(**values), so they are in kwargs
        assert kwargs['session_id'] == custom_sid

        # Verify set_clause in on_conflict_do_update includes session_id
        # upsert_stmt = insert_stmt.on_conflict_do_update(..., set_=set_clause)
        args, kwargs = mock_insert_stmt.on_conflict_do_update.call_args
        set_clause = kwargs['set_']

        # It should be referring to the excluded column
        assert 'session_id' in set_clause
