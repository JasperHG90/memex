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
async def test_kv_put_and_get(kv):
    """Put a KV entry, then retrieve it by key."""
    unique = str(uuid4())[:8]
    key = f'global:test:kv:{unique}'

    entry = await kv.put(key=key, value='test-value')
    assert entry is not None
    assert entry.key == key
    assert entry.value == 'test-value'

    retrieved = await kv.get(key=key)
    assert retrieved is not None
    assert retrieved.value == 'test-value'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_put_upsert_updates_value(kv):
    """Putting the same key twice should update the value."""
    unique = str(uuid4())[:8]
    key = f'global:test:upsert:{unique}'

    await kv.put(key=key, value='original')
    entry = await kv.put(key=key, value='updated')

    assert entry.value == 'updated'

    retrieved = await kv.get(key=key)
    assert retrieved is not None
    assert retrieved.value == 'updated'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_put_rejects_unnamespaced_key(kv):
    """Put should reject keys without a valid namespace prefix."""
    with pytest.raises(ValueError, match='namespace prefix'):
        await kv.put(key='bare:key', value='nope')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_delete_existing(kv):
    """Delete an existing KV entry."""
    unique = str(uuid4())[:8]
    key = f'global:test:delete:{unique}'

    await kv.put(key=key, value='to-delete')
    deleted = await kv.delete(key=key)
    assert deleted is True

    # Verify it's gone
    result = await kv.get(key=key)
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_delete_nonexistent(kv):
    """Delete returns False for non-existent key."""
    deleted = await kv.delete(key=f'global:nonexistent-{uuid4()}')
    assert deleted is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_all(kv):
    """list_entries without namespaces returns all entries."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'global:test:list:a:{unique}', value='a')
    await kv.put(key=f'user:test:list:b:{unique}', value='b')

    entries = await kv.list_entries()
    keys = [e.key for e in entries]
    assert f'global:test:list:a:{unique}' in keys
    assert f'user:test:list:b:{unique}' in keys


# ---------------------------------------------------------------------------
# KV namespace filtering
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_namespace_filter(kv):
    """list_entries with namespaces filters to matching prefixes."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'global:test:ns:{unique}', value='global-val')
    await kv.put(key=f'user:test:ns:{unique}', value='user-val')
    await kv.put(key=f'project:myproj:ns:{unique}', value='proj-val')

    # Filter to global only
    entries = await kv.list_entries(namespaces=['global'])
    keys = [e.key for e in entries]
    assert f'global:test:ns:{unique}' in keys
    assert f'user:test:ns:{unique}' not in keys
    assert f'project:myproj:ns:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_multiple_namespaces(kv):
    """list_entries with multiple namespaces returns entries from all specified."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'global:test:multi:{unique}', value='g')
    await kv.put(key=f'user:test:multi:{unique}', value='u')
    await kv.put(key=f'project:proj:multi:{unique}', value='p')

    entries = await kv.list_entries(namespaces=['global', 'user'])
    keys = [e.key for e in entries]
    assert f'global:test:multi:{unique}' in keys
    assert f'user:test:multi:{unique}' in keys
    assert f'project:proj:multi:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_project_namespace_with_id(kv):
    """list_entries with project:<id> namespace filters correctly."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'project:github.com/user/repo:setting:{unique}', value='val1')
    await kv.put(key=f'project:github.com/other/repo:setting:{unique}', value='val2')

    entries = await kv.list_entries(namespaces=['project:github.com/user/repo'])
    keys = [e.key for e in entries]
    assert f'project:github.com/user/repo:setting:{unique}' in keys
    assert f'project:github.com/other/repo:setting:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_exclude_prefix(kv):
    """list_entries with exclude_prefix filters out matching keys."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'global:test:excl:{unique}', value='keep')
    await kv.put(key=f'project:proj:excl:{unique}', value='exclude')

    entries = await kv.list_entries(exclude_prefix='project:')
    keys = [e.key for e in entries]
    assert f'global:test:excl:{unique}' in keys
    assert f'project:proj:excl:{unique}' not in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_list_key_prefix(kv):
    """list_entries with key_prefix returns only matching keys."""
    unique = str(uuid4())[:8]
    await kv.put(key=f'project:myproj:{unique}:vault', value='myvault')
    await kv.put(key=f'project:other:{unique}:vault', value='othervault')
    await kv.put(key=f'global:lang:go:{unique}', value='gopref')

    entries = await kv.list_entries(key_prefix=f'project:myproj:{unique}:')
    keys = [e.key for e in entries]
    assert f'project:myproj:{unique}:vault' in keys
    assert f'project:other:{unique}:vault' not in keys
    assert f'global:lang:go:{unique}' not in keys


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

    await kv.put(key=f'global:emb:a:{unique}', value='close', embedding=emb1)
    await kv.put(key=f'global:emb:b:{unique}', value='far', embedding=emb2)

    # Search with query embedding close to emb1
    results = await kv.search(query_embedding=[0.1] * 384, limit=5)
    assert len(results) >= 1
    # First result should be the closer entry
    assert results[0].value == 'close'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kv_search_with_namespace_filter(kv):
    """Search with namespace filter restricts results."""
    unique = str(uuid4())[:8]

    emb = [0.5] * 384
    await kv.put(key=f'global:emb:ns:{unique}', value='global-emb', embedding=emb)
    await kv.put(key=f'user:emb:ns:{unique}', value='user-emb', embedding=emb)

    results = await kv.search(query_embedding=emb, namespaces=['global'], limit=10)
    keys = [r.key for r in results]
    assert f'global:emb:ns:{unique}' in keys
    assert f'user:emb:ns:{unique}' not in keys


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
