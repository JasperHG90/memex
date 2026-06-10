"""Unit tests for the V7 ``MemexAPI.experiential`` facade.

The facade is a thin delegation layer over :class:`ExperientialRepository`
and :class:`ExperientialSearchService`. These tests cover the *wiring*
(``api.experiential.create`` actually calls the repository, etc.) — the
behaviour itself is covered by ``test_experiential_repository.py`` and
the integration tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_common.experiential_schemas import (
    ExperientialBriefingCards,
    ExperientialDerivationQueueDTO,
    ExperientialEntryCreate,
    ExperientialEntryDTO,
    ExperientialEntryUpdate,
    ExperientialSearchRequest,
    ExperientialSearchResponse,
)
from memex_core.services.experiential_repository import (
    ExperientialRepository,
)
from memex_core.services.experiential_search_service import (
    ExperientialSearchService,
)


def _fake_api() -> MagicMock:
    """Build a MagicMock that pretends to be a MemexAPI just enough
    to exercise the ``MemexAPIExperientialFacade`` delegation methods.

    The facade reads from ``self._api._experiential_repo`` and
    ``self._api._experiential_search``; we set those explicitly.
    """
    api = MagicMock()
    api._experiential_repo = AsyncMock(spec=ExperientialRepository)
    api._experiential_search = AsyncMock(spec=ExperientialSearchService)
    return api


@pytest.mark.asyncio
async def test_facade_create_delegates_to_repository():
    """``api.experiential.create(payload)`` forwards to the repository."""
    from memex_core.api import MemexAPIExperientialFacade

    api = _fake_api()
    payload = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='create_alembic',
        context='postgres',
        title='x',
        summary='y',
    )
    expected = MagicMock(spec=ExperientialEntryDTO)
    api._experiential_repo.create = AsyncMock(return_value=expected)

    facade = MemexAPIExperientialFacade(api)
    result = await facade.create(payload)

    api._experiential_repo.create.assert_awaited_once_with(payload)
    assert result is expected


@pytest.mark.asyncio
async def test_facade_update_passes_vault_id_through():
    """``api.experiential.update(...)`` propagates ``vault_id`` to the repo."""
    from memex_core.api import MemexAPIExperientialFacade

    api = _fake_api()
    entry_id = uuid4()
    vault_id = uuid4()
    payload = ExperientialEntryUpdate(summary='updated')
    api._experiential_repo.update = AsyncMock()

    facade = MemexAPIExperientialFacade(api)
    await facade.update(entry_id, payload, vault_id=vault_id)

    api._experiential_repo.update.assert_awaited_once_with(entry_id, payload, vault_id=vault_id)


@pytest.mark.asyncio
async def test_facade_search_delegates_to_search_service():
    """``api.experiential.search(request)`` forwards to the search service."""
    from memex_core.api import MemexAPIExperientialFacade

    api = _fake_api()
    request = ExperientialSearchRequest(query='how do I create a migration?')
    expected = MagicMock(spec=ExperientialSearchResponse)
    api._experiential_search.search = AsyncMock(return_value=expected)

    facade = MemexAPIExperientialFacade(api)
    result = await facade.search(request)

    api._experiential_search.search.assert_awaited_once_with(request)
    assert result is expected


@pytest.mark.asyncio
async def test_facade_briefing_cards_delegates_with_default_limit():
    """``api.experiential.briefing_cards(keys)`` uses the default cap."""
    from memex_core.api import MemexAPIExperientialFacade

    api = _fake_api()
    api._experiential_search.briefing_cards = AsyncMock(
        return_value=MagicMock(spec=ExperientialBriefingCards)
    )

    facade = MemexAPIExperientialFacade(api)
    await facade.briefing_cards(['global', 'project:abc'])

    api._experiential_search.briefing_cards.assert_awaited_once_with(
        ['global', 'project:abc'],
        scope=None,
        limit_per_context=5,
        vault_id=None,
    )


@pytest.mark.asyncio
async def test_facade_enqueue_derivation_passes_through():
    """``api.experiential.enqueue_derivation`` forwards to the repository."""
    from memex_core.api import MemexAPIExperientialFacade

    api = _fake_api()
    api._experiential_repo.enqueue_derivation = AsyncMock(
        return_value=MagicMock(spec=ExperientialDerivationQueueDTO)
    )

    facade = MemexAPIExperientialFacade(api)
    await facade.enqueue_derivation(
        vault_id=uuid4(),
        source_entry_ids=[uuid4(), uuid4()],
        target_kind='procedure',
        target_scope='project:abc',
    )

    assert api._experiential_repo.enqueue_derivation.await_args is not None
    call_kwargs = api._experiential_repo.enqueue_derivation.await_args.kwargs
    assert call_kwargs['target_kind'] == 'procedure'
    assert call_kwargs['target_scope'] == 'project:abc'
    assert call_kwargs['target_verb'] is None
    assert call_kwargs['target_context'] is None
    assert len(call_kwargs['source_entry_ids']) == 2
