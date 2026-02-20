import pytest
from unittest.mock import AsyncMock, MagicMock
from memex_core.api import Note


@pytest.mark.asyncio
async def test_ingest_new_note(api, mock_metastore, mock_session, mock_filestore):
    from memex_core.memory.sql_models import Vault
    from memex_common.config import GLOBAL_VAULT_ID

    # 1. Vault lookup (for resolve_vault_identifier)
    mock_vault_res = MagicMock()
    mock_vault_res.all.return_value = [Vault(id=GLOBAL_VAULT_ID, name='global')]

    # 2. Vault name lookup inside ingest (using session.get)
    mock_session.get.return_value = Vault(id=GLOBAL_VAULT_ID, name='global')

    # 3. Document existence check
    mock_doc_res = MagicMock()
    mock_doc_res.first.return_value = None

    mock_session.exec.side_effect = [mock_vault_res, mock_doc_res]

    # Mock MemoryEngine.retain
    api.memory.retain = AsyncMock()
    api.memory.retain.return_value = {'status': 'success', 'unit_ids': ['123']}

    note = Note(name='test', description='test desc', content=b'content')

    result = await api.ingest(note)

    assert result['status'] == 'success'
    assert result['unit_ids'] == ['123']

    # Verify transaction flow
    # mock_filestore.save is called inside AsyncTransaction
    api.memory.retain.assert_called()

    # Verify NO file saves for text-only note
    mock_filestore.save.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_existing_note(api, mock_session, mock_filestore):
    from memex_core.memory.sql_models import Vault
    from memex_common.config import GLOBAL_VAULT_ID

    # 1. Vault lookup
    mock_vault_res = MagicMock()
    mock_vault_res.all.return_value = [Vault(id=GLOBAL_VAULT_ID, name='global')]

    note = Note(name='test', description='test desc', content=b'content')

    # 2. Document existence check (exists with matching content_fingerprint)
    mock_doc_res = MagicMock()
    mock_doc_res.first.return_value = note.content_fingerprint

    mock_session.exec.side_effect = [mock_vault_res, mock_doc_res]

    result = await api.ingest(note)

    assert result['status'] == 'skipped'
    assert result['reason'] == 'idempotency_check'


@pytest.mark.asyncio
async def test_get_resource(api, mock_filestore):
    mock_filestore.load.return_value = b'data'

    result = await api.get_resource('some/path')

    assert result == b'data'
    mock_filestore.load.assert_called_with('some/path')


@pytest.mark.asyncio
async def test_list_documents(api, mock_session):
    from memex_core.memory.sql_models import Vault
    from memex_common.config import GLOBAL_VAULT_ID

    # 1. Vault lookup
    mock_vault_res = MagicMock()
    mock_vault_res.all.return_value = [Vault(id=GLOBAL_VAULT_ID, name='global')]

    # 2. List documents
    mock_docs_res = MagicMock()
    mock_docs_res.all.return_value = ['doc1', 'doc2']

    mock_session.exec.side_effect = [mock_vault_res, mock_docs_res]

    result = await api.list_documents()

    assert result == ['doc1', 'doc2']
