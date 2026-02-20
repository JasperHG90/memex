import pytest
from uuid import UUID
from memex_core.api import MemexAPI, Note
from memex_core.memory.sql_models import Document
from memex_common.config import MemexConfig
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore


@pytest.mark.asyncio
async def test_ingest_storage_refactor(
    metastore: AsyncBaseMetaStoreEngine,
    filestore: BaseAsyncFileStore,
    memex_config: MemexConfig,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
    session,
    fake_retain_factory,
):
    api = MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
    )
    await api.initialize()

    # Configure mocked memory engine to use our fake_retain
    api.memory.retain.side_effect = fake_retain_factory

    # Create a Note with content and auxiliary files
    content = b'# Hello World\nThis is a test note.'
    files = {'image.png': b'fake_image_data'}
    note = Note(
        name='Test Note',
        description='A test note for storage refactor.',
        content=content,
        files=files,
        tags=['test'],
    )

    # Ingest
    result = await api.ingest(note)
    assert result['status'] == 'success'
    doc_id_str = result['document_id']
    doc_id_uuid = UUID(doc_id_str)

    # 1. Verify Note NOT in FileStore (NOTE.md) under old path
    # Note: verify that we catch the specific exception raised by local filestore
    vault_name = 'global'  # default active vault name

    # We expect filestore.load to raise an error for the missing NOTE.md
    # Because we are using LocalAsyncFileStore (via fixture), it raises FileNotFoundError
    old_path = f'notes/{vault_name}/{doc_id_str}/NOTE.md'
    with pytest.raises(FileNotFoundError):
        await filestore.load(old_path)

    # 2. Verify Asset IN FileStore
    asset_path = f'assets/{vault_name}/{doc_id_str}/image.png'

    # DEBUG: List all files
    try:
        all_files = await filestore.glob('**/*')
        print(f'DEBUG: All files in filestore: {all_files}')
    except Exception as e:
        print(f'DEBUG: Failed to glob filestore: {e}')

    loaded_asset = await filestore.load(asset_path)
    assert loaded_asset == b'fake_image_data'

    # 3. Verify DB Document
    doc = await session.get(Document, doc_id_uuid)
    assert doc is not None
    assert doc.original_text == content.decode('utf-8')
    assert doc.assets == [asset_path]

    # 'filestore_path' in DB should point to the asset directory if assets exist
    expected_fs_path = f'assets/{vault_name}/{doc_id_str}'
    assert doc.filestore_path == expected_fs_path

    # 4. Verify get_document API
    doc_dto = await api.get_document(doc_id_uuid)
    assert doc_dto['original_text'] == content.decode('utf-8')
    assert doc_dto['assets'] == [asset_path]


@pytest.mark.asyncio
async def test_ingest_no_assets(
    metastore: AsyncBaseMetaStoreEngine,
    filestore: BaseAsyncFileStore,
    memex_config: MemexConfig,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
    session,
    fake_retain_factory,
):
    api = MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
    )
    await api.initialize()

    # Configure mocked memory engine to use our fake_retain
    api.memory.retain.side_effect = fake_retain_factory

    content = b'# Just Text'
    note = Note(
        name='Text Note', description='No assets here.', content=content, files=None, tags=['test']
    )

    result = await api.ingest(note)
    doc_id_str = result['document_id']
    doc_id_uuid = UUID(doc_id_str)

    doc = await session.get(Document, doc_id_uuid)
    assert doc.assets == []
    assert doc.filestore_path is None
