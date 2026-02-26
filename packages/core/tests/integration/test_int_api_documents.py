import pytest
from uuid import uuid4
from memex_core.memory.sql_models import Document, Vault
from memex_common.exceptions import ResourceNotFoundError
from memex_common.schemas import NoteDTO


@pytest.mark.asyncio
async def test_int_documents_api(api, metastore, memex_config):
    """
    Verify get_document and list_documents endpoints (via API wrapper or simulated call).
    """

    # 1. Setup Data
    vault_id = uuid4()
    doc_id = uuid4()
    doc_metadata = {'title': 'Test Doc', 'source': 'test', 'name': 'My Doc'}

    async with metastore.session() as session:
        session.add(Vault(id=vault_id, name='Test Vault'))
        await session.commit()

    async with metastore.session() as session:
        session.add(
            Document(
                id=doc_id,
                content_hash='hash',
                vault_id=vault_id,
                original_text='content',
                doc_metadata=doc_metadata,
            )
        )
        await session.commit()

    # Update config to search in the correct vault
    memex_config.server.active_vault = str(vault_id)

    # 2. Test get_note
    doc_result = await api.get_note(doc_id)
    assert doc_result['id'] == doc_id
    assert doc_result['doc_metadata'] == doc_metadata

    # Verify DTO Construction (Server Logic)
    dto = NoteDTO(
        id=doc_result['id'],
        name=doc_result['doc_metadata'].get('name') or doc_result['doc_metadata'].get('title'),
        created_at=doc_result['created_at'],
        vault_id=doc_result['vault_id'],
        doc_metadata=doc_result['doc_metadata'],
    )
    assert dto.name == 'My Doc'
    assert dto.id == doc_id
    assert dto.vault_id == vault_id

    # 3. Test list_notes
    docs_list = await api.list_notes()
    assert len(docs_list) == 1
    d = docs_list[0]

    # Server Logic for List
    list_dto = NoteDTO(
        id=d.id,
        name=d.doc_metadata.get('name') or d.doc_metadata.get('title'),
        created_at=d.created_at,
        vault_id=d.vault_id,
        doc_metadata=d.doc_metadata,
    )
    assert list_dto.name == 'My Doc'
    assert list_dto.id == doc_id


@pytest.mark.asyncio
async def test_int_get_note_page_index_with_data(api, metastore):
    """get_note_page_index returns the stored page_index when present."""
    from memex_common.config import GLOBAL_VAULT_ID

    page_index = {
        'toc': [
            {
                'id': 'section-1',
                'level': 1,
                'title': 'Introduction',
                'token_estimate': 120,
                'summary': {'what': 'Overview of the document'},
                'children': [
                    {
                        'id': 'section-1-1',
                        'level': 2,
                        'title': 'Background',
                        'token_estimate': 60,
                        'summary': {},
                        'children': [],
                    }
                ],
            }
        ]
    }
    doc_id = uuid4()

    async with metastore.session() as session:
        session.add(
            Document(
                id=doc_id,
                content_hash=str(uuid4()),
                vault_id=GLOBAL_VAULT_ID,
                original_text='content',
                page_index=page_index,
            )
        )
        await session.commit()

    result = await api.get_note_page_index(doc_id)

    assert result == page_index


@pytest.mark.asyncio
async def test_int_get_note_page_index_none_when_absent(api, metastore):
    """get_note_page_index returns None for documents without a page index."""
    from memex_common.config import GLOBAL_VAULT_ID

    doc_id = uuid4()

    async with metastore.session() as session:
        session.add(
            Document(
                id=doc_id,
                content_hash=str(uuid4()),
                vault_id=GLOBAL_VAULT_ID,
                original_text='content',
            )
        )
        await session.commit()

    result = await api.get_note_page_index(doc_id)

    assert result is None


@pytest.mark.asyncio
async def test_int_get_note_page_index_not_found(api):
    """get_note_page_index raises ResourceNotFoundError for missing documents."""
    missing_id = uuid4()

    with pytest.raises(ResourceNotFoundError):
        await api.get_note_page_index(missing_id)
