"""Synchronous wrappers for the procedural-plane client.

Hermes calls memory-provider methods synchronously, but
:class:`memex_common.client.RemoteMemexAPI` is async-only. This module
is the procedural-plane counterpart to the inline ``run_sync(...)`` calls
in :mod:`memex_hermes_plugin.memex.provider` — a thin layer of
sync-shaped functions that marshal each call onto the shared event loop
in :mod:`memex_hermes_plugin.memex.async_bridge`.

The eight methods here mirror the HTTP routes 1:1 (see V7 design §6):

* ``create``       → POST   /procedural
* ``upsert``       → POST   /procedural/upsert
* ``get``          → GET    /procedural/{id}
* ``get_by_identity`` → GET  /procedural/by-identity
* ``update``       → PATCH  /procedural/{id}
* ``deprecate``    → POST   /procedural/{id}/deprecate
* ``search``       → POST   /procedural/search
* ``briefing_cards`` → POST  /procedural/briefing-cards

All eight accept and return the same DTOs the async client does — the
sync wrapper is purely a coroutine-marshalling shim, not a re-shape.
Callers in Hermes can import this module and call straight through.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from memex_common.procedural_schemas import (
    ProceduralBriefingCards,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    ShortLabel,
)

from .async_bridge import run_sync

#: Default timeout for a procedural-plane round-trip. The plane's writes
#: are O(1) (a single INSERT) but the search path can do BM25+vector+RRF
#: fusion over many rows; 30s is the same budget the briefing fetch uses.
_DEFAULT_TIMEOUT: float = 30.0


def create(
    api: Any,
    payload: ProceduralEntryCreate,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Create a new procedural-plane entry. 409 on identity collision."""
    return run_sync(api.procedural_create(payload), timeout=timeout)


def upsert(
    api: Any,
    payload: ProceduralEntryCreate,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Idempotent write on the (kind, scope, verb, context) anchor."""
    return run_sync(api.procedural_upsert(payload), timeout=timeout)


def get(
    api: Any,
    entry_id: UUID,
    *,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Fetch a single entry by UUID. 404 on miss or vault mismatch."""
    return run_sync(api.procedural_get(entry_id, vault_id=vault_id), timeout=timeout)


def get_by_identity(
    api: Any,
    kind: ShortLabel,
    scope: ShortLabel,
    *,
    verb: ShortLabel | None = None,
    context: ShortLabel | None = None,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO | None:
    """Look up a single entry by its (kind, scope, verb, context) anchor.

    Returns ``None`` when the anchor is unbound — the cheap
    "did we already learn this?" probe. Never raises 404.
    """
    return run_sync(
        api.procedural_get_by_identity(kind, scope, verb=verb, context=context, vault_id=vault_id),
        timeout=timeout,
    )


def update(
    api: Any,
    entry_id: UUID,
    payload: ProceduralEntryUpdate,
    *,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Mutate an entry in place (appends a version row)."""
    return run_sync(
        api.procedural_update(entry_id, payload, vault_id=vault_id),
        timeout=timeout,
    )


def deprecate(
    api: Any,
    entry_id: UUID,
    *,
    superseded_by_id: UUID | None = None,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Soft-deprecate an entry (status → 'deprecated')."""
    return run_sync(
        api.procedural_deprecate(entry_id, superseded_by_id=superseded_by_id, vault_id=vault_id),
        timeout=timeout,
    )


def search(
    api: Any,
    request: ProceduralSearchRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralSearchResponse:
    """Hybrid BM25 + vector search (RRF-merged) across the procedural plane."""
    return run_sync(api.procedural_search(request), timeout=timeout)


def briefing_cards(
    api: Any,
    context_keys: list[ShortLabel],
    *,
    scope: ShortLabel | None = None,
    limit_per_context: int = 5,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralBriefingCards:
    """Pin-chain briefing cards for the session-briefing surface.

    One card per pinned entry, ordered by pin position. The list of
    ``context_keys`` MUST be non-empty (the HTTP route enforces
    ``min_length=1``); passing an empty list here is a programming
    error and will surface as a 422 from the server.
    """
    return run_sync(
        api.procedural_briefing_cards(
            context_keys, scope=scope, limit_per_context=limit_per_context
        ),
        timeout=timeout,
    )


__all__ = [
    'create',
    'upsert',
    'get',
    'get_by_identity',
    'update',
    'deprecate',
    'search',
    'briefing_cards',
]
