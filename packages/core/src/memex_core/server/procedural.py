"""Procedural-plane HTTP routes.

7 endpoints under ``/api/v1/procedural/*``. This is the third plane
(case / procedure / strategy entries) that sits alongside notes and
KV with its own identity anchor ``(kind, scope, verb, context)``.

The engine internals (SQLModel ``experiential_*`` tables, DTOs in
``memex_common.experiential_schemas``) ship under the legacy
``experiential`` prefix; the public HTTP surface is ``procedural``
because the agent-facing plane is the *procedural* plane.

Error mapping lives in ``_handle_error`` (server/common.py). The two
domain errors raised by :class:`ExperientialRepository` get explicit
branches there:

* :class:`ExperientialEntryNotFound` → 404
* :class:`ExperientialIdentityConflict` → 409

The search surface is one round-trip — the ``include_pin_chain`` +
``pin_contexts`` fields on the request envelope union textual / vector
hits with pinned entries in a single response. A separate
``/procedural/briefing-cards`` route serves the session-briefing
surface (one card per pin, no ranking).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query

from memex_common.exceptions import MemexError
from memex_common.experiential_schemas import (
    ExperientialBriefingCards,
    ExperientialEntryCreate,
    ExperientialEntryDTO,
    ExperientialEntryUpdate,
    ExperientialSearchRequest,
    ExperientialSearchResponse,
    ShortLabel,
)
from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


@router.post(
    '/procedural',
    response_model=ExperientialEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_create(
    request: Annotated[ExperientialEntryCreate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Create a new procedural-plane entry.

    Identity-anchor conflict on (kind, scope, verb, context) → 409.
    For idempotent writes (re-issue of the same anchor) use
    ``POST /procedural/upsert``.
    """
    try:
        return await api.experiential.create(request)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to create procedural-plane entry')


@router.get(
    '/procedural/by-identity',
    response_model=ExperientialEntryDTO | None,
    dependencies=[Depends(require_read)],
)
async def procedural_get_by_identity(
    api: Annotated[MemexAPI, Depends(get_api)],
    kind: Annotated[ShortLabel, Query(description='case | procedure | strategy')],
    scope: Annotated[ShortLabel, Query(description='Identity scope.')],
    verb: Annotated[
        ShortLabel | None,
        Query(description='Required for procedure/strategy; must be null for case.'),
    ] = None,
    context: Annotated[
        ShortLabel | None,
        Query(description='Optional context within (kind, scope, verb).'),
    ] = None,
    vault_id: Annotated[
        ShortLabel | None,
        Query(description='Vault UUID to scope the lookup.'),
    ] = None,
):
    """Look up a single entry by its (kind, scope, verb, context) anchor.

    Returns 200 with the entry, or 200 with ``null`` when the anchor
    is unbound (the "did we already learn this?" probe). The route
    never 404s — a miss is the cheap answer.
    """
    try:
        if kind == 'case':
            if verb is not None or context is not None:
                raise ValueError(
                    'kind=case requires verb=null and context=null '
                    '(cases do not carry an identity anchor beyond scope).'
                )
        else:
            if not verb:
                raise ValueError(f'kind={kind} requires verb (e.g. "rotate", "audit", "deploy").')

        results = await api.experiential.search(
            ExperientialSearchRequest(
                kind=kind,  # type: ignore[arg-type]
                scope=scope,
                status='published',
                limit=1,
            )
        )
        for hit in results.hits:
            if hit.entry.verb == verb and hit.entry.context == context:
                return hit.entry
        return None
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to look up procedural-plane entry by identity')


@router.get(
    '/procedural/{entry_id}',
    response_model=ExperientialEntryDTO,
    dependencies=[Depends(require_read)],
)
async def procedural_get(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: Annotated[
        ShortLabel | None,
        Query(description='Optional vault UUID; mismatch returns 404.'),
    ] = None,
):
    """Fetch a single entry by UUID."""
    try:
        vid: UUID | None = UUID(vault_id) if vault_id is not None else None
        return await api.experiential.get(entry_id, vault_id=vid)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get procedural-plane entry')


@router.patch(
    '/procedural/{entry_id}',
    response_model=ExperientialEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_update(
    entry_id: UUID,
    request: Annotated[ExperientialEntryUpdate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: Annotated[
        ShortLabel | None,
        Query(description='Optional vault UUID; mismatch returns 404.'),
    ] = None,
):
    """Mutate an entry in place (appends a version row)."""
    try:
        vid: UUID | None = UUID(vault_id) if vault_id is not None else None
        return await api.experiential.update(entry_id, request, vault_id=vid)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to update procedural-plane entry')


@router.post(
    '/procedural/{entry_id}/deprecate',
    response_model=ExperientialEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_deprecate(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    superseded_by_id: Annotated[
        UUID | None,
        Query(description='UUID of the entry that supersedes this one.'),
    ] = None,
    vault_id: Annotated[
        ShortLabel | None,
        Query(description='Optional vault UUID; mismatch returns 404.'),
    ] = None,
):
    """Soft-deprecate an entry (status → 'deprecated')."""
    try:
        return await api.experiential.deprecate(
            entry_id, superseded_by_id=superseded_by_id, vault_id=vault_id
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to deprecate procedural-plane entry')


@router.post(
    '/procedural/upsert',
    response_model=ExperientialEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_upsert(
    request: Annotated[ExperientialEntryCreate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Idempotent write on the identity anchor.

    Same anchor → UPDATE (new version row); new anchor → INSERT.
    Status is preserved (deprecated stays deprecated). For partial
    in-place edits use ``PATCH /procedural/{entry_id}``.
    """
    try:
        return await api.experiential.upsert(request)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to upsert procedural-plane entry')


@router.post(
    '/procedural/search',
    response_model=ExperientialSearchResponse,
    dependencies=[Depends(require_read)],
)
async def procedural_search(
    request: Annotated[ExperientialSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Hybrid BM25 + vector search (RRF-merged) across the procedural plane."""
    try:
        return await api.experiential.search(request)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Procedural-plane search failed')


@router.post(
    '/procedural/briefing-cards',
    response_model=ExperientialBriefingCards,
    dependencies=[Depends(require_read)],
)
async def procedural_briefing_cards(
    context_keys: Annotated[list[ShortLabel], Body(min_length=1)],
    api: Annotated[MemexAPI, Depends(get_api)],
    scope: Annotated[
        ShortLabel | None,
        Query(description='Restrict cards to a single scope.'),
    ] = None,
    limit_per_context: Annotated[
        int,
        Query(ge=1, le=20, description='Cap per-context card count (default 5).'),
    ] = 5,
):
    """Pin-chain briefing cards for the session-briefing surface.

    One card per pinned entry, ordered by pin position. Use for the
    "what you should know going in" block of a session briefing.
    """
    try:
        return await api.experiential.briefing_cards(
            context_keys, scope=scope, limit_per_context=limit_per_context
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to load briefing cards')
