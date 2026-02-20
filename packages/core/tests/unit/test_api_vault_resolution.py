import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from memex_core.memory.sql_models import Vault
from memex_common.exceptions import VaultNotFoundError, AmbiguousResourceError


@pytest.mark.asyncio
async def test_resolve_vault_identifier_valid_uuid(api, mock_session):
    """Test resolving a valid UUID string that exists."""
    target_uuid = uuid4()

    # Mock validate_vault_exists to return True
    # validate_vault_exists calls session.get(Vault, uuid)
    mock_session.get.return_value = Vault(id=target_uuid, name='Test Vault')

    resolved = await api.resolve_vault_identifier(str(target_uuid))
    assert resolved == target_uuid


@pytest.mark.asyncio
async def test_resolve_vault_identifier_valid_name(api, mock_session):
    """Test resolving a valid name that exists and is unique."""
    target_uuid = uuid4()
    vault_name = 'Research Projects'

    # 1. First checks if it's a UUID -> fails ValueError, proceeds to name lookup

    # 2. Name lookup: session.exec(select(Vault)...).all()
    mock_result = MagicMock()
    mock_result.all.return_value = [Vault(id=target_uuid, name=vault_name)]
    mock_session.exec.return_value = mock_result

    resolved = await api.resolve_vault_identifier(vault_name)
    assert resolved == target_uuid


@pytest.mark.asyncio
async def test_resolve_vault_identifier_not_found(api, mock_session):
    """Test resolving a name/ID that does not exist."""

    # 1. Try UUID format first
    random_uuid = str(uuid4())
    mock_session.get.return_value = None  # UUID lookup fails

    # 2. Try Name lookup
    mock_result = MagicMock()
    mock_result.all.return_value = []  # Name lookup fails
    mock_session.exec.return_value = mock_result

    with pytest.raises(VaultNotFoundError, match='not found'):
        await api.resolve_vault_identifier(random_uuid)

    # Test plain name
    with pytest.raises(VaultNotFoundError, match='not found'):
        await api.resolve_vault_identifier('NonExistentVault')


@pytest.mark.asyncio
async def test_resolve_vault_identifier_ambiguous_name(api, mock_session):
    """Test resolving a name that matches multiple vaults."""
    vault_name = 'Duplicate'
    v1 = Vault(id=uuid4(), name=vault_name)
    v2 = Vault(id=uuid4(), name=vault_name)

    mock_result = MagicMock()
    mock_result.all.return_value = [v1, v2]
    mock_session.exec.return_value = mock_result

    with pytest.raises(AmbiguousResourceError, match='Multiple vaults found'):
        await api.resolve_vault_identifier(vault_name)


@pytest.mark.asyncio
async def test_ingest_resolves_vault_from_config(api, mock_session):
    """Test that ingest uses resolve_vault_identifier for config.server.active_vault."""

    # Setup config with a name
    api.config.server.active_vault = 'MyVault'
    target_id = uuid4()

    # Mock resolution logic within ingest
    # Since we can't easily patch methods on the 'api' instance being tested,
    # we rely on mocking the DB calls that resolve_vault_identifier makes.

    # 1. Ingest calls resolve_vault_identifier("MyVault")
    # -> It's not a UUID
    # -> Queries DB for name
    mock_result_vault = MagicMock()
    mock_result_vault.all.return_value = [Vault(id=target_id, name='MyVault')]

    # 2. Ingest calls idempotency check (select(1)...)
    mock_result_exists = MagicMock()
    mock_result_exists.first.return_value = None

    # We need session.exec to return different things for different calls
    # 1. Vault Search -> mock_result_vault
    # 2. Idempotency -> mock_result_exists
    mock_session.exec.side_effect = [mock_result_vault, mock_result_exists]

    # Mock Transaction and MemoryEngine
    from memex_core.api import Note

    note = Note(name='test', description='d', content=b'c')

    # Mock MemoryEngine.retain
    api.memory.retain = AsyncMock()
    api.memory.retain.return_value = {'unit_ids': [], 'usage': {}, 'touched_entities': []}

    # Mock Transaction context
    txn = MagicMock()
    txn.__aenter__.return_value = txn
    txn.db_session = mock_session  # pass through

    with patch('memex_core.api.AsyncTransaction', return_value=txn):
        await api.ingest(note)

    # Verify retain was called with the resolved vault_id
    # We check the 'contents' arg passed to retain
    call_args = api.memory.retain.call_args
    assert call_args is not None
    contents = call_args.kwargs['contents']
    assert len(contents) == 1
    assert contents[0].vault_id == target_id
