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

import time
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
from memex_core.metrics import (
    PROCEDURAL_BRIEFING_CARDS_TOTAL,
    PROCEDURAL_IDENTITY_CONFLICT_TOTAL,
    PROCEDURAL_OPERATIONS_TOTAL,
    PROCEDURAL_SEARCH_DURATION_SECONDS,
)
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api
from memex_core.services.experiential_repository import (
    ExperientialEntryNotFound,
    ExperientialIdentityConflict,
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
    (case | procedure | strategy); ``exc`` is the exception that aborted
    the call (None means success).
    """
    if exc is None:
        outcome = 'success'
    elif isinstance(exc, ExperientialIdentityConflict):
        outcome = 'identity_conflict'
    elif isinstance(exc, ExperientialEntryNotFound):
        outcome = 'not_found'
    else:
        outcome = 'error'
    PROCEDURAL_OPERATIONS_TOTAL.labels(operation=operation, kind=kind, outcome=outcome).inc()


def _record_identity_conflict(api: MemexAPI, kind: str, exc: ExperientialIdentityConflict) -> None:
    """Record a 409 collision. The ``mode`` label reflects the operator's
    configured behaviour — useful for distinguishing "operator flipped to
    upsert and conflicts disappeared" from "operator flipped to upsert
    and a stealth overwrite ran"."""
    mode = api.config.server.memory.procedural.identity_conflict_mode
    PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind=kind, mode=mode).inc()


def _record_search(
    api: MemexAPI,
    kind: str | None,
    response: ExperientialSearchResponse,
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
        _record_write_outcome('create', request.kind, e)
        if isinstance(e, ExperientialIdentityConflict):
            _record_identity_conflict(api, request.kind, e)
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
        # Read path — no operation-counter increment (the histogram is
        # the search-path SLA; reads-by-id are too cheap to be
        # observability-worthy on their own).
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
        result = await api.experiential.update(entry_id, request, vault_id=vid)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        # `update` body does not carry `kind` — pull it from the entry
        # being updated so the counter label stays bounded. We don't
        # care if the read itself 404s; we record what we can.
        try:
            existing = await api.experiential.get(entry_id, vault_id=vid)
            kind_label = existing.kind
        except Exception:
            kind_label = 'unknown'
        _record_write_outcome('update', kind_label, e)
        raise _handle_error(e, 'Failed to update procedural-plane entry')
    _record_write_outcome('update', result.kind, None)
    return result


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
        result = await api.experiential.deprecate(
            entry_id, superseded_by_id=superseded_by_id, vault_id=vault_id
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        _record_write_outcome('deprecate', 'unknown', e)
        raise _handle_error(e, 'Failed to deprecate procedural-plane entry')
    _record_write_outcome('deprecate', result.kind, None)
    return result


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
    with trace_span(
        'memex_core.procedural',
        'procedural.upsert',
        {
            'procedural.kind': request.kind,
            'procedural.scope': request.scope or '',
        },
    ):
        try:
            return await api.experiential.upsert(request)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            _record_write_outcome('upsert', request.kind, e)
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
            response = await api.experiential.search(request)
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
    with trace_span(
        'memex_core.procedural',
        'procedural.briefing_cards',
        {
            'procedural.context_count': str(len(context_keys)),
            'procedural.scope': scope or '',
        },
    ):
        try:
            result = await api.experiential.briefing_cards(
                context_keys, scope=scope, limit_per_context=limit_per_context
            )
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Failed to load briefing cards')
        # Count requests by context-key count, not card count. Card
        # count is recoverable from the response body; request shape
        # is the load-bearing observability dimension.
        _record_briefing_cards(context_keys)
        return result
