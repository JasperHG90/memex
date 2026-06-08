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

    # Content vault ids the wildcard expands to (settable per test).
    svc._content_ids = []

    async def _scope(identifiers, include_system_vaults=False):
        ids: list = []
        seen: set = set()

        def _add(u):
            if u not in seen:
                seen.add(u)
                ids.append(u)

        identifiers = identifiers or []
        has_wildcard = any(str(v) == '*' for v in identifiers)
        named = [v for v in identifiers if str(v) != '*']
        for n in named:
            _add(await _resolve(str(n)))
        if has_wildcard or not named:
            for c in svc._content_ids:
                _add(c)
        return ids

    svc.resolve_vault_scope = _scope
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


@pytest.mark.asyncio
async def test_search_wildcard_resolves_to_content_vaults(vault_service):
    """When vault_ids=['*'], search resolves to content vaults only."""
    v1, v2 = uuid4(), uuid4()
    vault_service._content_ids = [v1, v2]

    config = MagicMock()
    config.server.default_reader_vault = 'default'

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

    await svc.search(query='test', vault_ids=['*'])

    request = memory.recall.call_args[0][1]
    assert set(request.vault_ids) == {v1, v2}, 'Should contain all vaults'


@pytest.mark.asyncio
async def test_search_notes_wildcard_resolves_to_content_vaults(vault_service):
    """When vault_ids=['*'], search_notes resolves to content vaults only."""
    v1, v2 = uuid4(), uuid4()
    vault_service._content_ids = [v1, v2]

    config = MagicMock()
    config.server.default_reader_vault = 'default'
    config.server.document.mmr_lambda = None

    doc_search = AsyncMock()
    doc_search.search = AsyncMock(return_value=[])

    metastore = MagicMock()
    mock_session = AsyncMock()
    metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    svc = SearchService(
        metastore=metastore,
        config=config,
        lm=MagicMock(),
        memory=MagicMock(),
        doc_search=doc_search,
        vaults=vault_service,
    )

    await svc.search_notes(query='test', vault_ids=['*'])

    doc_search.search.assert_called_once()
    request = doc_search.search.call_args[0][1]
    assert set(request.vault_ids) == {v1, v2}, 'Should contain all vaults'
