"""Tests for SearchService vault resolution — default_reader_vault fallback."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.search import SearchService


@pytest.fixture
def vault_service():
    """Mock VaultService that maps names/IDs to UUIDs."""
    svc = AsyncMock()
    svc._resolved = {}

    async def _resolve(identifier: str):
        from uuid import UUID

        try:
            return UUID(identifier)
        except ValueError:
            # Map name to a deterministic UUID
            if identifier not in svc._resolved:
                svc._resolved[identifier] = uuid4()
            return svc._resolved[identifier]

    svc.resolve_vault_identifier = _resolve
    return svc


@pytest.fixture
def search_service(vault_service):
    """SearchService with mocked dependencies."""
    config = MagicMock()
    config.server.default_reader_vault = 'my-vault'

    memory = AsyncMock()
    memory.recall = AsyncMock(return_value=([], None))

    svc = SearchService(
        metastore=MagicMock(),
        config=config,
        lm=MagicMock(),
        memory=memory,
        doc_search=MagicMock(),
        vaults=vault_service,
    )
    # Patch session context
    svc.metastore.session = MagicMock()
    return svc


@pytest.mark.asyncio
async def test_search_resolves_default_reader_vault_when_no_vault_ids(
    search_service, vault_service
):
    """When vault_ids=None, search should resolve default_reader_vault."""
    # Mock the session context manager
    mock_session = AsyncMock()
    search_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    search_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)
    search_service.memory.recall = AsyncMock(return_value=([], None))

    await search_service.search(query='test query', vault_ids=None)

    # Verify recall was called and the request had 1 vault_id (default_reader_vault)
    search_service.memory.recall.assert_called_once()
    request = search_service.memory.recall.call_args[0][1]
    assert len(request.vault_ids) == 1, 'Should have default_reader_vault only'


@pytest.mark.asyncio
async def test_search_uses_explicit_vault_ids_when_provided(search_service, vault_service):
    """When vault_ids are explicitly provided, default_reader_vault should NOT be added."""
    mock_session = AsyncMock()
    search_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    search_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)
    search_service.memory.recall = AsyncMock(return_value=([], None))

    explicit_id = uuid4()
    await search_service.search(query='test', vault_ids=[explicit_id])

    request = search_service.memory.recall.call_args[0][1]
    assert len(request.vault_ids) == 1, 'Should only have the explicitly provided vault'
    assert request.vault_ids[0] == explicit_id


@pytest.mark.asyncio
async def test_search_default_reader_vault_only(vault_service):
    """When no vault_ids provided, only default_reader_vault is used."""
    config = MagicMock()
    config.server.default_reader_vault = 'solo-vault'

    memory = AsyncMock()
    memory.recall = AsyncMock(return_value=([], None))

    svc = SearchService(
        metastore=MagicMock(),
        config=config,
        lm=MagicMock(),
        memory=memory,
        doc_search=MagicMock(),
        vaults=vault_service,
    )
    mock_session = AsyncMock()
    svc.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    svc.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    await svc.search(query='test')

    request = memory.recall.call_args[0][1]
    assert len(request.vault_ids) == 1, 'Should only have the default reader vault'
