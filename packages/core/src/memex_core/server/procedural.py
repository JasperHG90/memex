"""Procedural-plane HTTP routes.

Endpoints under ``/api/v1/procedural/*``. The procedural plane holds
procedure and strategy entries alongside notes and KV, with its own
identity anchor ``(kind, scope, verb, context)``: procedure ≡ (scope,
verb, context); strategy ≡ (scope, verb, NULL) — §18.1. Cases are
NOTES (``role='case'`` in the hidden system vault), not rows here.

Error mapping lives in ``_handle_error`` (server/common.py). The two
domain errors raised by :class:`ProceduralRepository` get explicit
branches there:

* :class:`ProceduralEntryNotFound` → 404
* :class:`ProceduralIdentityConflict` → 409

The search surface is one round-trip — the ``include_pin_chain`` +
``pin_contexts`` fields on the request envelope union textual / vector
hits with pinned entries in a single response. A separate
``/procedural/briefing-cards`` route serves the session-briefing
surface (one card per pin, no ranking).
"""

import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query

from memex_common.exceptions import MemexError
from memex_common.procedural_schemas import (
    ProceduralBriefingCards,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralEntryVersionDTO,
    ProceduralPinCreate,
    ProceduralPinDTO,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    ShortLabel,
)
from memex_core.api import MemexAPI
from memex_core.metrics import (
    PROCEDURAL_BRIEFING_CARDS_TOTAL,
    PROCEDURAL_IDENTITY_CONFLICT_TOTAL,
    PROCEDURAL_OPERATIONS_TOTAL,
    PROCEDURAL_SEARCH_DURATION_SECONDS,
)
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api
from memex_core.services.procedural_repository import (
    ProceduralEntryNotFound,
    ProceduralIdentityConflict,
)
from memex_core.tracing import trace_span

router = APIRouter(prefix='/api/v1')


# ---------------------------------------------------------------------------
# Metrics + tracing helpers
# ---------------------------------------------------------------------------
# Cardinality: the ``kind`` label is a 3-value Literal; ``operation`` and
# ``outcome`` are short enums. ``streams`` (search) is 4 values. The
# total label space is O(dozens), not O(rows) — safe for Prometheus.
#
# We expose the helpers as module-level functions so unit tests can
# drive them directly without spinning up the FastAPI app.

#: Bucket boundaries for the `context_count_bucket` label on the briefing
#: card counter. The right edge is inclusive (matches Prometheus's
#: conventional "le" semantics for histogram buckets, applied here to a
#: counter's integer label). The catch-all ``"10+"`` bucket prevents
#: unbounded label growth from a misbehaving caller.
_CONTEXT_COUNT_BUCKETS = ('1', '2', '3', '4', '5', '6_to_10', '10+')


def _context_count_bucket(n: int) -> str:
    """Return the bucket name for a context-key count of ``n``."""
    if n <= 0:
        return '1'  # Defensive — the route rejects min_length=0, but be safe.
    if n <= 5:
        return str(n)
    if n <= 10:
        return '6_to_10'
    return '10+'


def _record_write_outcome(operation: str, kind: str, exc: BaseException | None) -> None:
    """Increment the write-operation counter with the right outcome label.

    Called from the route's ``except`` block, before ``_handle_error`` is
    invoked. ``operation`` is the verb; ``kind`` is the entity class
    (procedure | strategy); ``exc`` is the exception that aborted
    the call (None means success).
    """
    if exc is None:
        outcome = 'success'
    elif isinstance(exc, ProceduralIdentityConflict):
        outcome = 'identity_conflict'
    elif isinstance(exc, ProceduralEntryNotFound):
        outcome = 'not_found'
    else:
        outcome = 'error'
    PROCEDURAL_OPERATIONS_TOTAL.labels(operation=operation, kind=kind, outcome=outcome).inc()


def _record_identity_conflict(api: MemexAPI, kind: str, exc: ProceduralIdentityConflict) -> None:
    """Record a 409 collision. The ``mode`` label reflects the operator's
    configured behaviour — useful for distinguishing "operator flipped to
    upsert and conflicts disappeared" from "operator flipped to upsert
    and a stealth overwrite ran"."""
    mode = api.config.server.memory.procedural.identity_conflict_mode
    PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind=kind, mode=mode).inc()


def _record_search(
    api: MemexAPI,
    kind: str | None,
    response: ProceduralSearchResponse,
    duration_seconds: float,
) -> None:
    """Record the search histogram. The ``streams`` label summarises
    which of (bm25, vector, pin) actually contributed to the result
    set — a request that only hits one stream still completes; the
    label preserves that signal for SRE dashboards.

    Per-hit ``matched_via`` is a single value: 'bm25' (bm25-only),
    'vector' (vector-only), 'rrf' (both streams hit), or 'pin' (pin
    chain match). A request-level stream summary unions these values
    across hits, with an `rrf` hit counting as both bm25 AND vector
    participation (the per-hit label is intentionally not the union).
    """
    has_bm25 = False
    has_vector = False
    has_pin = False
    for hit in response.hits:
        via = hit.matched_via
        if via == 'bm25':
            has_bm25 = True
        elif via == 'vector':
            has_vector = True
        elif via == 'rrf':
            has_bm25 = True
            has_vector = True
        elif via == 'pin':
            has_pin = True
    if has_bm25 and has_vector:
        streams = 'rrf'
    elif has_pin:
        streams = 'pin_only'
    elif has_bm25:
        streams = 'bm25_only'
    elif has_vector:
        streams = 'vector_only'
    else:
        streams = 'bm25_only'  # empty result — label as the cheapest bucket.
    PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind=kind or 'all', streams=streams).observe(
        duration_seconds
    )
    # Silence the unused-arg linter on `api`; the kind label only
    # narrows the histogram, it does not need config-aware resolution.
    del api


def _record_briefing_cards(context_keys: list[ShortLabel]) -> None:
    """Increment the briefing-card counter once per request, with the
    context-key count as a bucketed label. Card count itself is
    observably recoverable from the response body — we count requests
    here to keep the metric scrape cost O(1)."""
    bucket = _context_count_bucket(len(context_keys))
    PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket=bucket).inc()


@router.post(
    '/procedural',
    response_model=ProceduralEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_create(
    request: Annotated[ProceduralEntryCreate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Create a new procedural-plane entry.

    Identity-anchor conflict on (kind, scope, verb, context) → 409,
    UNLESS the operator set ``server.memory.procedural.identity_conflict_mode
    = 'upsert'``, in which case a colliding create transparently updates
    the existing row (the same effect as ``POST /procedural/upsert``).
    """
    conflict_mode = api.config.server.memory.procedural.identity_conflict_mode
    try:
        if conflict_mode == 'upsert':
            # Operator opted into silent overwrite-on-collision.
            result = await api.procedural.upsert(request)
        else:
            result = await api.procedural.create(request)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        _record_write_outcome('create', request.kind, e)
        if isinstance(e, ProceduralIdentityConflict):
            _record_identity_conflict(api, request.kind, e)
        raise _handle_error(e, 'Failed to create procedural-plane entry')
    # Mirror the success-path increment :meth:`procedural_update`
    # emits — the metric is otherwise biased toward failure counts.
    _record_write_outcome('create', request.kind, None)
    return result


@router.get(
    '/procedural/by-identity',
    response_model=ProceduralEntryDTO | None,
    dependencies=[Depends(require_read)],
)
async def procedural_get_by_identity(
    api: Annotated[MemexAPI, Depends(get_api)],
    kind: Annotated[ShortLabel, Query(description='procedure | strategy')],
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

    The query is a direct identity-anchor SELECT against the partial
    unique index ``uq_procedural_identity`` (not a fuzzy search).
    A previous implementation routed through ``api.procedural.search``
    with no query text, which short-circuited to an empty response and
    silently returned ``None`` for every anchor — that regression broke
    the agent's read-before-write flow and is the reason this route
    now calls the repository's exact-anchor method.
    """
    try:
        if kind not in ('procedure', 'strategy'):
            raise ValueError(
                f'kind={kind!r} is not on the procedural plane '
                "(procedure | strategy). Cases are notes (role='case') — "
                'submit them via the cases surface, not here.'
            )
        if not verb:
            raise ValueError(f'kind={kind} requires verb (e.g. "rotate", "audit", "deploy").')
        if kind == 'strategy' and context is not None:
            raise ValueError(
                'kind=strategy requires context=null — a strategy is the '
                'projection over all procedures sharing (scope, verb) (§18.1).'
            )
        if kind == 'procedure' and not context:
            raise ValueError('kind=procedure requires context (e.g. "creds", "nomad").')

        return await api.procedural.get_by_identity(
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            verb=verb,
            context=context,
            vault_id=vault_id,
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to look up procedural-plane entry by identity')


@router.get(
    '/procedural/pins',
    response_model=list[ProceduralPinDTO],
    dependencies=[Depends(require_read)],
)
async def procedural_list_pins(
    api: Annotated[MemexAPI, Depends(get_api)],
    context_key: Annotated[ShortLabel, Query(description='Pin-chain context key.')],
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
):
    """Pins for one context, position ascending (curation surface)."""
    try:
        return await api.procedural.list_pins(context_key, limit=limit)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list procedural pins')


@router.get(
    '/procedural/{entry_id}',
    response_model=ProceduralEntryDTO,
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
        return await api.procedural.get(entry_id, vault_id=vid)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        # Read path — no operation-counter increment (the histogram is
        # the search-path SLA; reads-by-id are too cheap to be
        # observability-worthy on their own).
        raise _handle_error(e, 'Failed to get procedural-plane entry')


@router.patch(
    '/procedural/{entry_id}',
    response_model=ProceduralEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_update(
    entry_id: UUID,
    request: Annotated[ProceduralEntryUpdate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: Annotated[
        ShortLabel | None,
        Query(description='Optional vault UUID; mismatch returns 404.'),
    ] = None,
):
    """Mutate an entry in place (appends a version row)."""
    try:
        vid: UUID | None = UUID(vault_id) if vault_id is not None else None
        result = await api.procedural.update(entry_id, request, vault_id=vid)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        # `update` body does not carry `kind` — pull it from the entry
        # being updated so the counter label stays bounded. We don't
        # care if the read itself 404s; we record what we can.
        try:
            existing = await api.procedural.get(entry_id, vault_id=vid)
            kind_label = existing.kind
        except Exception:
            kind_label = 'unknown'
        _record_write_outcome('update', kind_label, e)
        raise _handle_error(e, 'Failed to update procedural-plane entry')
    _record_write_outcome('update', result.kind, None)
    return result


@router.post(
    '/procedural/{entry_id}/deprecate',
    response_model=ProceduralEntryDTO,
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
        result = await api.procedural.deprecate(
            entry_id, superseded_by_id=superseded_by_id, vault_id=vault_id
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        _record_write_outcome('deprecate', 'unknown', e)
        raise _handle_error(e, 'Failed to deprecate procedural-plane entry')
    _record_write_outcome('deprecate', result.kind, None)
    return result


@router.post(
    '/procedural/derive',
    dependencies=[Depends(require_write)],
)
async def procedural_derive(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description='Max pending derivation tasks to drain this call.'),
    ] = 10,
):
    """Drain pending derivation tasks (cases → procedure, procedures →
    strategy). Runs the §9 distillation passes synchronously and writes the
    derived entries. Used by the background scheduler and exposed here for
    ops + deterministic eval triggering. Returns the completed queue ids."""
    try:
        completed = await api.procedural.derive_pending(limit=limit)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to run procedural derivation')
    return {'completed': len(completed), 'queue_ids': [str(q) for q in completed]}


@router.post(
    '/procedural/{entry_id}/pin',
    response_model=ProceduralPinDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_pin(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    context_key: Annotated[ShortLabel, Body(embed=True)],
    position: Annotated[int | None, Body(embed=True, ge=0)] = None,
    pinned_by: Annotated[str | None, Body(embed=True)] = None,
):
    """Pin an entry into a context-binding chain (§19.8).

    ``position`` omitted appends to the end of the chain. 422 when the
    context already holds the cap (10) or the context key violates the
    grammar (global | project:<id> | app:<id>).
    """
    try:
        payload = ProceduralPinCreate(
            context_key=context_key,
            entry_id=entry_id,
            position=position,
            pinned_by=pinned_by,
        )
        # Entry must exist — surface a 404 rather than an FK violation.
        await api.procedural.get(entry_id)
        return await api.procedural.pin(payload)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to pin procedural-plane entry')


@router.delete(
    '/procedural/{entry_id}/pin',
    response_model=dict,
    dependencies=[Depends(require_write)],
)
async def procedural_unpin(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    context_key: Annotated[ShortLabel, Query(description='Pin-chain context key.')],
):
    """Unpin an entry from a context. Idempotent — 200 with removed=0
    when no pin existed."""
    try:
        removed = await api.procedural.unpin(entry_id=entry_id, context_key=context_key)
        return {'entry_id': str(entry_id), 'context_key': context_key, 'removed': removed}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to unpin procedural-plane entry')


@router.get(
    '/procedural/{entry_id}/versions',
    response_model=list[ProceduralEntryVersionDTO],
    dependencies=[Depends(require_read)],
)
async def procedural_list_versions(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """The entry's uncapped version ledger, newest first (diff /
    rollback surface, §18.8)."""
    try:
        return await api.procedural.list_versions(entry_id)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list procedural-plane entry versions')


@router.post(
    '/procedural/{entry_id}/rollback',
    response_model=ProceduralEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_rollback(
    entry_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    version: Annotated[int, Body(embed=True, ge=1)],
    rolled_back_by: Annotated[str | None, Body(embed=True)] = None,
):
    """Non-destructive rollback: the requested snapshot is re-applied
    as a NEW version row; nothing is deleted (§18.8). 404 when the
    entry has no such version."""
    try:
        result = await api.procedural.rollback(entry_id, version, rolled_back_by=rolled_back_by)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        _record_write_outcome('rollback', 'unknown', e)
        raise _handle_error(e, 'Failed to roll back procedural-plane entry')
    _record_write_outcome('rollback', result.kind, None)
    return result


@router.post(
    '/procedural/upsert',
    response_model=ProceduralEntryDTO,
    dependencies=[Depends(require_write)],
)
async def procedural_upsert(
    request: Annotated[ProceduralEntryCreate, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Idempotent write on the identity anchor.

    Same anchor → UPDATE (new version row); new anchor → INSERT.
    Status is preserved (deprecated stays deprecated). For partial
    in-place edits use ``PATCH /procedural/{entry_id}``.
    """
    with trace_span(
        'memex_core.procedural',
        'procedural.upsert',
        {
            'procedural.kind': request.kind,
            'procedural.scope': request.scope or '',
        },
    ):
        try:
            result = await api.procedural.upsert(request)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            _record_write_outcome('upsert', request.kind, e)
            raise _handle_error(e, 'Failed to upsert procedural-plane entry')
        # Mirror the success-path increment :meth:`procedural_update`
        # emits — the metric is otherwise biased toward failure counts.
        _record_write_outcome('upsert', request.kind, None)
        return result


@router.post(
    '/procedural/search',
    response_model=ProceduralSearchResponse,
    dependencies=[Depends(require_read)],
)
async def procedural_search(
    request: Annotated[ProceduralSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Hybrid BM25 + vector search (RRF-merged) across the procedural plane."""
    with trace_span(
        'memex_core.procedural',
        'procedural.search',
        {
            'procedural.kind': request.kind or 'all',
            'procedural.scope': request.scope or '',
        },
    ):
        started = time.monotonic()
        try:
            response = await api.procedural.search(request)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            # Record nothing on error — the SLA signal is meaningless
            # when the search blew up before producing a result set.
            raise _handle_error(e, 'Procedural-plane search failed')
        _record_search(
            api,
            request.kind,
            response,
            time.monotonic() - started,
        )
        return response


@router.post(
    '/procedural/briefing-cards',
    response_model=ProceduralBriefingCards,
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
    vault_id: Annotated[
        UUID | None,
        Query(description='Restrict cards to a single vault (multi-tenancy guard).'),
    ] = None,
):
    """Pin-chain briefing cards for the session-briefing surface.

    One card per pinned entry, ordered by pin position. Use for the
    "what you should know going in" block of a session briefing.

    The ``vault_id`` parameter, when set, restricts the cards to
    entries in the caller's vault — the multi-tenancy guardrail.
    Leaving it None returns the global result set (operator-only).
    """
    with trace_span(
        'memex_core.procedural',
        'procedural.briefing_cards',
        {
            'procedural.context_count': str(len(context_keys)),
            'procedural.scope': scope or '',
        },
    ):
        try:
            result = await api.procedural.briefing_cards(
                context_keys,
                scope=scope,
                limit_per_context=limit_per_context,
                vault_id=vault_id,
            )
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Failed to load briefing cards')
        # Count requests by context-key count, not card count. Card
        # count is recoverable from the response body; request shape
        # is the load-bearing observability dimension.
        _record_briefing_cards(context_keys)
        return result
