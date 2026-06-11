"""Unit tests for the ``MemexAPI.procedural`` facade.

The facade is a thin delegation layer over :class:`ProceduralRepository`
and :class:`ProceduralSearchService`. These tests cover the *wiring*
(``api.procedural.create`` actually calls the repository, etc.) — the
behaviour itself is covered by ``test_procedural_repository.py`` and
the integration tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_common.procedural_schemas import (
    ProceduralBriefingCards,
    ProceduralDerivationQueueDTO,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
)
from memex_core.services.procedural_repository import (
    ProceduralRepository,
)
from memex_core.services.procedural_search_service import (
    ProceduralSearchService,
)


def _fake_api() -> MagicMock:
    """Build a MagicMock that pretends to be a MemexAPI just enough
    to exercise the ``MemexAPIProceduralFacade`` delegation methods.

    The facade reads from ``self._api._procedural_repo`` and
    ``self._api._procedural_search``; we set those explicitly.
    """
    api = MagicMock()
    api._procedural_repo = AsyncMock(spec=ProceduralRepository)
    api._procedural_search = AsyncMock(spec=ProceduralSearchService)
    return api


@pytest.mark.asyncio
async def test_facade_create_delegates_to_repository():
    """``api.procedural.create(payload)`` forwards to the repository."""
    from memex_core.api import MemexAPIProceduralFacade

    api = _fake_api()
    payload = ProceduralEntryCreate(
        vault_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='create_alembic',
        context='postgres',
        title='x',
        summary='y',
        trigger='creating an alembic migration for postgres',
    )
    expected = MagicMock(spec=ProceduralEntryDTO)
    api._procedural_repo.create = AsyncMock(return_value=expected)
    # The facade embeds the trigger at write time (§18.7) and threads
    # the vector into the repository call.
    api._procedural_search.embed_trigger = AsyncMock(return_value=[0.1] * 384)

    facade = MemexAPIProceduralFacade(api)
    result = await facade.create(payload)

    api._procedural_search.embed_trigger.assert_awaited_once_with(payload.trigger)
    api._procedural_repo.create.assert_awaited_once_with(payload, trigger_embedding=[0.1] * 384)
    assert result is expected


@pytest.mark.asyncio
async def test_facade_update_passes_vault_id_through():
    """``api.procedural.update(...)`` propagates ``vault_id`` to the repo."""
    from memex_core.api import MemexAPIProceduralFacade

    api = _fake_api()
    entry_id = uuid4()
    vault_id = uuid4()
    # No trigger change → the facade computes no embedding and threads
    # trigger_embedding=None (the repo nulls any stale vector).
    payload = ProceduralEntryUpdate(summary='updated')
    api._procedural_repo.update = AsyncMock()

    facade = MemexAPIProceduralFacade(api)
    await facade.update(entry_id, payload, vault_id=vault_id)

    api._procedural_repo.update.assert_awaited_once_with(
        entry_id, payload, vault_id=vault_id, trigger_embedding=None
    )


@pytest.mark.asyncio
async def test_facade_search_delegates_to_search_service():
    """``api.procedural.search(request)`` forwards to the search service."""
    from memex_core.api import MemexAPIProceduralFacade

    api = _fake_api()
    request = ProceduralSearchRequest(query='how do I create a migration?')
    expected = MagicMock(spec=ProceduralSearchResponse)
    api._procedural_search.search = AsyncMock(return_value=expected)

    facade = MemexAPIProceduralFacade(api)
    result = await facade.search(request)

    api._procedural_search.search.assert_awaited_once_with(request)
    assert result is expected


@pytest.mark.asyncio
async def test_facade_briefing_cards_delegates_with_default_limit():
    """``api.procedural.briefing_cards(keys)`` uses the default cap."""
    from memex_core.api import MemexAPIProceduralFacade

    api = _fake_api()
    api._procedural_search.briefing_cards = AsyncMock(
        return_value=MagicMock(spec=ProceduralBriefingCards)
    )

    facade = MemexAPIProceduralFacade(api)
    await facade.briefing_cards(['global', 'project:abc'])

    api._procedural_search.briefing_cards.assert_awaited_once_with(
        ['global', 'project:abc'],
        scope=None,
        limit_per_context=5,
        vault_id=None,
    )


@pytest.mark.asyncio
async def test_facade_enqueue_derivation_passes_through():
    """``api.procedural.enqueue_derivation`` forwards to the repository."""
    from memex_core.api import MemexAPIProceduralFacade

    api = _fake_api()
    api._procedural_repo.enqueue_derivation = AsyncMock(
        return_value=MagicMock(spec=ProceduralDerivationQueueDTO)
    )

    facade = MemexAPIProceduralFacade(api)
    await facade.enqueue_derivation(
        vault_id=uuid4(),
        source_entry_ids=[uuid4(), uuid4()],
        target_kind='procedure',
        target_scope='project:abc',
    )

    assert api._procedural_repo.enqueue_derivation.await_args is not None
    call_kwargs = api._procedural_repo.enqueue_derivation.await_args.kwargs
    assert call_kwargs['target_kind'] == 'procedure'
    assert call_kwargs['target_scope'] == 'project:abc'
    assert call_kwargs['target_verb'] is None
    assert call_kwargs['target_context'] is None
    assert len(call_kwargs['source_entry_ids']) == 2
