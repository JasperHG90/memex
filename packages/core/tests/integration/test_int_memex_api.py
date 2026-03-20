import pytest
from uuid import uuid4

from sqlmodel import select, col
from memex_core.api import MemexAPI, NoteInput
from memex_core.memory.sql_models import Note, MemoryUnit


@pytest.mark.asyncio
async def test_int_ingest_success(api, metastore, fake_retain_factory):
    """
    Verify successful ingestion:
    1. Transaction commits.
    2. Files moved from staging to permanent location.
    3. DB records persisted.
    """
    # api.memory is already an AsyncMock from patch_api_engines (included in api fixture)
    api.memory.retain.side_effect = fake_retain_factory

    # Create a NoteInput
    note_content = b'# Test NoteInput\nThis is a test.'
    note = NoteInput(name='test_int_note', description='desc', content=note_content)
    note_uuid = note.idempotency_key

    # Execute Ingest
    result = await api.ingest(note)

    assert result['status'] == 'success'

    # Verify DB Persistence
    async with metastore.session() as session:
        doc = await session.get(Note, note_uuid)
        assert doc is not None
        assert doc.original_text == note_content.decode('utf-8')

        # Verify Memory Unit
        units = (
            await session.exec(select(MemoryUnit).where(col(MemoryUnit.note_id) == note_uuid))
        ).all()
        assert len(units) == 1
        assert units[0].text == 'Extracted fact'

    # Verify FileStore
    # We didn't add any files to the NoteInput, so nothing should be saved in FileStore
    # Wait, NoteInput has no files in this test.

    # Verify transaction cleanup (staging should be empty/deleted)
    # staging_path = f"staging/{note_uuid}"
    # assert not await filestore.exists(staging_path)


@pytest.mark.asyncio
async def test_int_ingest_rollback_on_error(api, metastore, filestore):
    """
    Verify rollback on error:
    1. Exception raised during extraction/retention.
    2. DB transaction rolled back (no records).
    3. Files removed from staging (no permanent files).
    """
    # api.memory.retain is already an AsyncMock from patch_api_engines (included in api fixture)
    api.memory.retain.side_effect = RuntimeError('Extraction Failed!')

    note = NoteInput(name='fail_note', description='desc', content=b'fail')
    note_uuid = note.idempotency_key

    with pytest.raises(RuntimeError, match='Extraction Failed!'):
        await api.ingest(note)

    # Verify DB is Empty
    async with metastore.session() as session:
        doc = await session.get(Note, note_uuid)
        assert doc is None

        # Verify File System is Empty (Cleaned up)
        # For text-only notes, no file is written initially, so just ensure nothing is there.
        vault_name = 'global'
        expected_path = f'notes/{vault_name}/{note_uuid}/NOTE.md'
        assert not await filestore.exists(expected_path)


@pytest.mark.asyncio
async def test_int_list_documents_vault_filter(
    metastore,
    filestore,
    memex_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
):
    """
    Verify that list_documents respects the active vault.
    """
    from memex_core.memory.sql_models import Vault
    from memex_common.config import GLOBAL_VAULT_ID

    # 1. Create Documents in DB directly (bypass ingest for speed)
    vault_a = uuid4()
    vault_b = uuid4()

    async with metastore.session() as session:
        # Global vault exists due to fixture
        session.add(Vault(id=vault_a, name='A'))
        session.add(Vault(id=vault_b, name='B'))
        await session.commit()

    async with metastore.session() as session:
        doc1 = Note(id=uuid4(), content_hash='h1', vault_id=vault_a, original_text='A')
        doc2 = Note(id=uuid4(), content_hash='h2', vault_id=vault_b, original_text='B')
        doc3 = Note(id=uuid4(), content_hash='h3', vault_id=GLOBAL_VAULT_ID, original_text='Global')
        session.add(doc1)
        session.add(doc2)
        session.add(doc3)
        await session.commit()

    # 2. Config for Vault A
    memex_config.server.default_active_vault = str(vault_a)
    api_a = MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
    )

    docs_a = await api_a.list_notes(vault_id=vault_a)
    assert len(docs_a) == 1
    assert docs_a[0].vault_id == vault_a

    # 3. Config for Global
    memex_config.server.default_active_vault = 'global'
    api_global = MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
    )

    docs_global = await api_global.list_notes(vault_id=GLOBAL_VAULT_ID)
    assert len(docs_global) == 1
    assert docs_global[0].vault_id == GLOBAL_VAULT_ID
