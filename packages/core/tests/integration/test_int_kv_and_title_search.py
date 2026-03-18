"""Integration tests for KV lifecycle and title search with real PostgreSQL.

Requires Docker (testcontainers).
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Note, Vault
from memex_core.services.kv import KVService


@pytest.fixture
def kv(metastore, filestore, memex_config):
    """KVService wired to the real test database."""
    return KVService(metastore=metastore, filestore=filestore, config=memex_config)


# ---------------------------------------------------------------------------
# KV Lifecycle: write -> get -> list -> delete
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_put_and_get_global(kv):
    """Put a global KV entry, then retrieve it by key."""
    unique = str(uuid4())[:8]
    key = f'test:kv:{unique}'

    entry = await kv.put(vault_id=None, key=key, value='test-value')
    assert entry is not None
    assert entry.key == key
    assert entry.value == 'test-value'
    assert entry.vault_id is None

    retrieved = await kv.get(key=key)
    assert retrieved is not None
    assert retrieved.value == 'test-value'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_put_upsert_updates_value(kv):
    """Putting the same key twice should update the value."""
    unique = str(uuid4())[:8]
    key = f'test:upsert:{unique}'

    await kv.put(vault_id=None, key=key, value='original')
    entry = await kv.put(vault_id=None, key=key, value='updated')

    assert entry.value == 'updated'

    retrieved = await kv.get(key=key)
    assert retrieved is not None
    assert retrieved.value == 'updated'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_delete_existing(kv):
    """Delete an existing KV entry."""
    unique = str(uuid4())[:8]
    key = f'test:delete:{unique}'

    await kv.put(vault_id=None, key=key, value='to-delete')
    deleted = await kv.delete(key=key)
    assert deleted is True

    # Verify it's gone
    result = await kv.get(key=key)
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_delete_nonexistent(kv):
    """Delete returns False for non-existent key."""
    deleted = await kv.delete(key=f'nonexistent-{uuid4()}')
    assert deleted is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_global(kv):
    """list_entries without vault_id returns global entries."""
    unique = str(uuid4())[:8]
    await kv.put(vault_id=None, key=f'test:list:a:{unique}', value='a')
    await kv.put(vault_id=None, key=f'test:list:b:{unique}', value='b')

    entries = await kv.list_entries()
    keys = [e.key for e in entries]
    assert f'test:list:a:{unique}' in keys
    assert f'test:list:b:{unique}' in keys


# ---------------------------------------------------------------------------
# KV list filtering: exclude_prefix and key_prefix
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_exclude_prefix(kv):
    """list_entries with exclude_prefix filters out matching keys."""
    unique = str(uuid4())[:8]
    await kv.put(vault_id=None, key=f'agents:proj:{unique}', value='project-setting')
    await kv.put(vault_id=None, key=f'lang:python:{unique}', value='user-pref')

    entries = await kv.list_entries(exclude_prefix='agents:')
    keys = [e.key for e in entries]
    assert f'lang:python:{unique}' in keys
    assert f'agents:proj:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_key_prefix(kv):
    """list_entries with key_prefix returns only matching keys."""
    unique = str(uuid4())[:8]
    await kv.put(vault_id=None, key=f'agents:myproj:{unique}:vault', value='myvault')
    await kv.put(vault_id=None, key=f'agents:other:{unique}:vault', value='othervault')
    await kv.put(vault_id=None, key=f'lang:go:{unique}', value='gopref')

    entries = await kv.list_entries(key_prefix=f'agents:myproj:{unique}:')
    keys = [e.key for e in entries]
    assert f'agents:myproj:{unique}:vault' in keys
    assert f'agents:other:{unique}:vault' not in keys
    assert f'lang:go:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_prefix_combined_with_vault(kv, session):
    """exclude_prefix and key_prefix work alongside vault_id."""
    unique = str(uuid4())[:8]

    vault_id = uuid4()
    vault = Vault(id=vault_id, name=f'test-vault-prefix-{unique}', description='Test')
    session.add(vault)
    await session.commit()

    await kv.put(vault_id=None, key=f'agents:proj:{unique}', value='agent-setting')
    await kv.put(vault_id=None, key=f'user:pref:{unique}', value='user-pref')
    await kv.put(vault_id=vault_id, key=f'domain:config:{unique}', value='domain-val')

    # With vault_id, exclude agents: prefix — should get global + vault-scoped, minus agents:
    entries = await kv.list_entries(vault_id=vault_id, exclude_prefix='agents:')
    keys = [e.key for e in entries]
    assert f'user:pref:{unique}' in keys
    assert f'domain:config:{unique}' in keys
    assert f'agents:proj:{unique}' not in keys


# ---------------------------------------------------------------------------
# KV vault scoping: global vs vault-specific, fallback
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_vault_scoping(kv, session):
    """Vault-scoped entries are separate from global entries with the same key."""
    unique = str(uuid4())[:8]
    key = f'test:scope:{unique}'

    # Create a test vault
    vault_id = uuid4()
    vault = Vault(id=vault_id, name=f'test-vault-{unique}', description='Test')
    session.add(vault)
    await session.commit()

    # Put global
    await kv.put(vault_id=None, key=key, value='global-value')
    # Put vault-specific
    await kv.put(vault_id=vault_id, key=key, value='vault-value')

    # Get vault-specific should return vault value
    result = await kv.get(key=key, vault_id=vault_id)
    assert result is not None
    assert result.value == 'vault-value'

    # Get global should return global value
    result_global = await kv.get(key=key)
    assert result_global is not None
    assert result_global.value == 'global-value'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_vault_fallback_to_global(kv, session):
    """When vault_id is given but no vault-specific entry exists, fall back to global."""
    unique = str(uuid4())[:8]
    key = f'test:fallback:{unique}'

    vault_id = uuid4()
    vault = Vault(id=vault_id, name=f'test-vault-fb-{unique}', description='Test')
    session.add(vault)
    await session.commit()

    # Only put global
    await kv.put(vault_id=None, key=key, value='global-only')

    # Get with vault_id should fall back to global
    result = await kv.get(key=key, vault_id=vault_id)
    assert result is not None
    assert result.value == 'global-only'
    assert result.vault_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_with_vault_includes_global(kv, session):
    """list_entries with vault_id should include both vault-scoped and global."""
    unique = str(uuid4())[:8]

    vault_id = uuid4()
    vault = Vault(id=vault_id, name=f'test-vault-list-{unique}', description='Test')
    session.add(vault)
    await session.commit()

    await kv.put(vault_id=None, key=f'test:global:{unique}', value='g')
    await kv.put(vault_id=vault_id, key=f'test:vault:{unique}', value='v')

    entries = await kv.list_entries(vault_id=vault_id)
    keys = [e.key for e in entries]
    assert f'test:global:{unique}' in keys
    assert f'test:vault:{unique}' in keys


# ---------------------------------------------------------------------------
# KV with embeddings
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_put_with_embedding_and_search(kv):
    """Put entries with embeddings and search by vector similarity."""
    unique = str(uuid4())[:8]

    # Create entries with embeddings (384-dim)
    emb1 = [0.1] * 384
    emb2 = [0.9] * 384

    await kv.put(vault_id=None, key=f'emb:a:{unique}', value='close', embedding=emb1)
    await kv.put(vault_id=None, key=f'emb:b:{unique}', value='far', embedding=emb2)

    # Search with query embedding close to emb1
    results = await kv.search(query_embedding=[0.1] * 384, limit=5)
    assert len(results) >= 1
    # First result should be the closer entry
    assert results[0].value == 'close'


# ---------------------------------------------------------------------------
# find_notes_by_title (trigram search)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def notes_service(metastore, filestore, memex_config, session):
    """NoteService wired to the real test database."""
    from memex_core.services.notes import NoteService
    from memex_core.services.vaults import VaultService

    vault_svc = VaultService(metastore=metastore, filestore=filestore, config=memex_config)
    svc = NoteService(
        metastore=metastore, filestore=filestore, config=memex_config, vaults=vault_svc
    )

    # Insert some notes with titles for trigram search
    unique = str(uuid4())[:8]
    notes_data = [
        ('Meeting notes from Monday standup', unique),
        ('Architecture design document', unique),
        ('Python development guidelines', unique),
        ('Quick reference for Git commands', unique),
    ]
    for title, tag in notes_data:
        note = Note(
            id=uuid4(),
            title=f'{title} [{tag}]',
            content_hash=str(uuid4()),
            vault_id=GLOBAL_VAULT_ID,
            original_text=f'Content of {title} {uuid4()}',
            status='active',
        )
        session.add(note)
    await session.commit()

    return svc, unique


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_exact_match(notes_service):
    """find_notes_by_title should find notes with exact title match."""
    svc, unique = notes_service

    results = await svc.find_notes_by_title(query=f'Meeting notes from Monday standup [{unique}]')
    assert len(results) >= 1
    assert any('Meeting notes' in r['title'] for r in results)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_fuzzy_match(notes_service):
    """find_notes_by_title should find notes with fuzzy/approximate titles."""
    svc, unique = notes_service

    # Search with slight variation — trigram should still match
    results = await svc.find_notes_by_title(query='Architecture design doc', limit=5)
    # Should find our "Architecture design document [...]" note
    assert len(results) >= 1
    assert any('Architecture' in r['title'] for r in results)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_no_results(notes_service):
    """find_notes_by_title should return empty list for no matches."""
    svc, _ = notes_service

    results = await svc.find_notes_by_title(query='xyznonexistent1234567890')
    assert results == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_respects_limit(notes_service):
    """find_notes_by_title should respect the limit parameter."""
    svc, unique = notes_service

    results = await svc.find_notes_by_title(query=unique, limit=2)
    assert len(results) <= 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_result_shape(notes_service):
    """find_notes_by_title results should have the expected keys."""
    svc, unique = notes_service

    results = await svc.find_notes_by_title(query=f'Meeting notes [{unique}]')
    if results:
        r = results[0]
        assert 'note_id' in r
        assert 'title' in r
        assert 'score' in r
        assert 'vault_id' in r
        assert 'created_at' in r
        assert 'status' in r
        assert isinstance(r['score'], float)
        assert 0 < r['score'] <= 1.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_notes_by_title_filters_by_vault(notes_service, session):
    """find_notes_by_title should filter by vault_ids when provided."""
    svc, unique = notes_service

    other_vault_id = uuid4()
    vault = Vault(id=other_vault_id, name=f'other-vault-{unique}', description='Other')
    session.add(vault)
    await session.commit()

    # Search with a vault that has no notes
    results = await svc.find_notes_by_title(
        query=f'Meeting notes [{unique}]', vault_ids=[other_vault_id]
    )
    assert results == []

    # Search with global vault should find results
    results = await svc.find_notes_by_title(
        query=f'Meeting notes [{unique}]', vault_ids=[GLOBAL_VAULT_ID]
    )
    assert len(results) >= 1
