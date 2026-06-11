"""Procedural Plane — search service.

Hybrid search across ``procedural_entries``:

* **BM25** over the generated ``search_tsvector`` column (English config,
  Porter-stemmed). Backed by the GIN index from migration 061.
* **Vector cosine** over ``trigger_embedding`` — the single vector leg
  (design §6/§18.7; spike §19.1). Backed by the partial HNSW index from
  migration 064.
* **Reciprocal Rank Fusion (RRF)** merges the two ranked lists.
* **Pin chain** optionally unions entries pinned at the supplied
  ``pin_contexts`` list — used by the session-briefing surface.

Embedding generation is offloaded to a thread via the shared semaphore
(``get_embedding_semaphore``) and the ``_instrument('embed')`` wrapper,
mirroring the public ``MemexAPI.embed_text`` path. Two call sites: the
search-time *query* embedding here, and the write-time *trigger*
embedding the facade requests via :meth:`embed_trigger` (§18.7 —
embeddings are computed by the caller and stored at write time; there
is no lazy backfill).

Briefing cards
--------------

``briefing_cards()`` is the read path for the session briefing's
procedural slot. The caller supplies a list of context keys
(``["global", "project:<id>", "app:<agent>"]``); the service returns
one card per pin, ordered by ``position`` ascending. Pin positions
are 0-based and the briefing shows them in chain order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import literal_column
from sqlmodel import col, func, select

from memex_common.procedural_schemas import (
    ProceduralBriefingCard,
    ProceduralBriefingCards,
    ProceduralEntryDTO,
    ProceduralSearchHit,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    KindLiteral,
    ShortLabel,
)
from memex_core.instrument import _instrument
from memex_core.memory.models.protocols import EmbeddingsModel
from memex_core.memory.retrieval._offload import (
    get_embedding_call_timeout,
    get_embedding_semaphore,
)
from memex_core.memory.sql_models import (
    ProceduralEntry as DBProceduralEntry,
)
from memex_core.memory.sql_models import (
    ProceduralPin as DBProceduralPin,
)
from memex_core.memory.sql_models import (
    ProceduralStatus as DBProceduralStatus,
)
from memex_core.services.procedural_repository import ProceduralRepository
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.procedural_search')

# RRF constant. Mirrors K_RRF in document_search.py:81. Keep in sync if
# either side is retuned.
K_RRF = 60


def _mw_boost(entry: object) -> float:
    """§18.5 Memory-Worth boost from an entry's outcome counters — the same
    Beta-Bernoulli posterior the unit ranker uses (``compute_mw_boost`` by
    identity, not a fork). Cold-start (0/0) → 1.0 (neutral)."""
    from memex_core.services.outcomes import compute_mw_boost

    return compute_mw_boost(
        int(getattr(entry, 'success_count', 0) or 0),
        int(getattr(entry, 'failure_count', 0) or 0),
    )


# Status filter default — matches the briefing semantics where only
# published entries are surfaced to agents.
DEFAULT_STATUS = 'published'

# Default over-fetch factor for the BM25 + vector streams before RRF.
# We pull N*overfetch rows from each side so RRF has enough candidates
# to actually fuse — without overfetch, top-10 results may have
# non-overlapping BM25 and vector lists.
_OVERFETCH = 5


class ProceduralSearchError(Exception):
    """Base error for the procedural search service."""


class ProceduralSearchService:
    """Hybrid search + briefing cards for the procedural plane."""

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        repository: ProceduralRepository,
        embedding_model: EmbeddingsModel,
    ) -> None:
        self._metastore = metastore
        self._repository = repository
        self._embedding_model = embedding_model

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def search(self, request: ProceduralSearchRequest) -> ProceduralSearchResponse:
        """Run a hybrid BM25 + vector search and RRF-fuse the results.

        At least one of ``request.query`` or ``request.pin_contexts`` must
        contribute — otherwise the response is empty and ``total=0``.

        When ``request.vault_id`` is set, every BM25, vector, and
        pin-chain candidate is restricted to that vault. This is the
        multi-tenancy guardrail — leaving it None returns the global
        result set, which is only appropriate for operator/CLI
        contexts.
        """
        started = time.monotonic()

        if not request.query and not (request.include_pin_chain and request.pin_contexts):
            return ProceduralSearchResponse(took_ms=(time.monotonic() - started) * 1000)

        bm25_hits: list[tuple[UUID, float]] = []
        vector_hits: list[tuple[UUID, float]] = []

        if request.query:
            try:
                bm25_hits = await self._bm25_search(
                    request.query,
                    scope=request.scope,
                    kind=request.kind,
                    status=request.status,
                    vault_id=request.vault_id,
                    limit=request.limit * _OVERFETCH,
                )
            except Exception:
                logger.exception('procedural BM25 search failed; continuing with vector only')

            try:
                query_vec = await self._embed_text(request.query)
                vector_hits = await self._vector_search(
                    query_vec,
                    scope=request.scope,
                    kind=request.kind,
                    status=request.status,
                    vault_id=request.vault_id,
                    limit=request.limit * _OVERFETCH,
                )
            except Exception:
                logger.exception('procedural vector search failed; continuing with BM25 only')

        bm25_rank = {eid: rank for rank, (eid, _) in enumerate(bm25_hits)}
        vector_rank = {eid: rank for rank, (eid, _) in enumerate(vector_hits)}

        # RRF score: sum of 1/(K + rank + 1) over streams that hit.
        scores: dict[UUID, float] = {}
        for eid, rank in bm25_rank.items():
            scores[eid] = scores.get(eid, 0.0) + request.bm25_weight / (K_RRF + rank + 1)
        for eid, rank in vector_rank.items():
            scores[eid] = scores.get(eid, 0.0) + request.vector_weight / (K_RRF + rank + 1)

        # Optional pin chain union. Pins get a fixed weight — they are
        # operator-curated, not score-derived.
        pin_hits: dict[UUID, int] = {}
        if request.include_pin_chain and request.pin_contexts:
            pin_hits = await self._pin_lookup(
                request.pin_contexts,
                scope=request.scope,
                kind=request.kind,
                status=request.status,
                vault_id=request.vault_id,
            )
            for eid, position in pin_hits.items():
                # Position 0 is the top of the chain — give it the full
                # weight, decay by position. Always strictly positive.
                scores[eid] = scores.get(eid, 0.0) + 1.0 / (1 + position)

        if not scores:
            return ProceduralSearchResponse(took_ms=(time.monotonic() - started) * 1000)

        # Fetch entries and build hits. Pull a small overshoot so we
        # can truncate after the DTO conversion in case the rank
        # computation produced ties that re-order on hydration.
        ordered_ids = sorted(scores, key=lambda k: scores[k], reverse=True)
        fetch_ids = ordered_ids[: request.limit]
        entries_by_id = {e.id: e for e in await self._repository.get_many(fetch_ids)}

        hits: list[ProceduralSearchHit] = []
        for eid in fetch_ids:
            entry = entries_by_id.get(eid)
            if entry is None:
                continue
            matched_via: str
            pin_position: int | None = None
            if eid in pin_hits and eid not in bm25_rank and eid not in vector_rank:
                matched_via = 'pin'
                pin_position = pin_hits[eid]
            elif eid in bm25_rank and eid in vector_rank:
                matched_via = 'rrf'
            elif eid in bm25_rank:
                matched_via = 'bm25'
            else:
                matched_via = 'vector'

            hits.append(
                ProceduralSearchHit(
                    entry=entry,
                    score=scores[eid] * _mw_boost(entry),
                    bm25_rank=bm25_rank.get(eid),
                    vector_rank=vector_rank.get(eid),
                    matched_via=matched_via,  # type: ignore[arg-type]
                    pin_position=pin_position,
                )
            )

        # If the hydration dropped someone (e.g. status changed mid-search),
        # re-fill from the rank-ordered list to honour request.limit.
        if len(hits) < request.limit:
            missing = [eid for eid in ordered_ids if eid not in entries_by_id]
            extra = await self._repository.get_many(missing[: request.limit - len(hits)])
            for entry in extra:
                hits.append(
                    ProceduralSearchHit(
                        entry=entry,
                        score=scores[entry.id] * _mw_boost(entry),
                        bm25_rank=bm25_rank.get(entry.id),
                        vector_rank=vector_rank.get(entry.id),
                        matched_via=(
                            'pin'
                            if entry.id in pin_hits
                            and entry.id not in bm25_rank
                            and entry.id not in vector_rank
                            else 'rrf'
                        ),
                        pin_position=pin_hits.get(entry.id),
                    )
                )

        # §18.5: re-rank by the Memory-Worth-boosted score. The boost is the
        # Beta-Bernoulli posterior over success/failure (compute_mw_boost),
        # applied post-RRF. Re-sort within the fetched set — a boundary
        # entry just outside the overshoot can't be pulled in, but within
        # the candidate set a well-worn procedure outranks an unproven peer.
        hits.sort(key=lambda h: h.score, reverse=True)

        return ProceduralSearchResponse(
            hits=hits[: request.limit],
            total=len(ordered_ids),
            truncated=len(ordered_ids) > len(hits),
            took_ms=(time.monotonic() - started) * 1000,
        )

    async def briefing_cards(
        self,
        context_keys: list[ShortLabel],
        *,
        scope: ShortLabel | None = None,
        limit_per_context: int = 5,
        vault_id: UUID | None = None,
    ) -> ProceduralBriefingCards:
        """Return one briefing card per pin in the requested contexts.

        Pins are returned in (context_key, position) order so the briefing
        can render the chain in priority order. The implicit chain
        ``global → project:<id> → app:<agent>`` is the caller's job to
        materialise — this method does not auto-expand.

        When ``vault_id`` is set, only entries in that vault are surfaced.
        Leaving it None returns the global result set, which is only
        appropriate for operator/CLI paths that need a cross-vault view.
        """
        if not context_keys:
            return ProceduralBriefingCards()

        cards: list[ProceduralBriefingCard] = []
        total_pinned = 0

        async with self._metastore.session() as session:
            stmt = (
                select(DBProceduralPin, DBProceduralEntry)
                .join(
                    DBProceduralEntry,
                    col(DBProceduralPin.entry_id) == col(DBProceduralEntry.id),
                )
                .where(col(DBProceduralPin.context_key).in_(context_keys))
                .where(col(DBProceduralEntry.status) == DBProceduralStatus.PUBLISHED)
                .order_by(col(DBProceduralPin.position).asc())
            )
            if scope is not None:
                stmt = stmt.where(col(DBProceduralEntry.scope) == scope)
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)

            results = (await session.exec(stmt)).all()

            # Order by the caller's chain priority — the SQL can't know it,
            # so we sort by the index of each pin's context in
            # ``context_keys`` (global → project:<id> → app:<consumer>),
            # then by position. NOT alphabetical context_key (which would
            # scramble the precedence narrative).
            chain_rank = {ck: i for i, ck in enumerate(context_keys)}
            ordered = sorted(
                results,
                key=lambda pe: (
                    chain_rank.get(pe[0].context_key, len(context_keys)),
                    pe[0].position,
                ),
            )

            # Dedup across contexts (§19.8): an entry pinned in two chain
            # contexts surfaces ONCE, at its highest-priority (earliest in
            # the chain) context. Per-context cap still applies.
            per_context_count: dict[str, int] = {ck: 0 for ck in context_keys}
            seen_entries: set = set()
            for pin, entry in ordered:
                if entry.id in seen_entries:
                    continue
                if per_context_count.get(pin.context_key, 0) >= limit_per_context:
                    continue
                seen_entries.add(entry.id)
                per_context_count[pin.context_key] = per_context_count.get(pin.context_key, 0) + 1
                total_pinned += 1
                cards.append(
                    ProceduralBriefingCard(
                        entry=ProceduralEntryDTO.model_validate(entry),
                        pin_position=int(pin.position),
                        context_key=pin.context_key,
                    )
                )

        return ProceduralBriefingCards(
            cards=cards,
            context_keys=context_keys,
            total_pinned=total_pinned,
        )

    # ------------------------------------------------------------------
    # Internal: BM25 (tsvector) search
    # ------------------------------------------------------------------

    async def _bm25_search(
        self,
        query: str,
        *,
        scope: ShortLabel | None,
        kind: KindLiteral | None,
        status: str,
        vault_id: UUID | None,
        limit: int,
    ) -> list[tuple[UUID, float]]:
        """Run a tsvector match + ts_rank_cd ordering.

        Returns ``[(entry_id, rank_score), ...]`` ordered by rank desc.
        The score is informational — the search caller uses rank position,
        not the score itself, for RRF.
        """
        entry = DBProceduralEntry
        tsquery = func.plainto_tsquery(literal_column("'english'::regconfig"), query)
        rank = func.ts_rank_cd(entry.search_tsvector, tsquery).label('rank')

        stmt = (
            select(col(entry.id), rank)
            .where(entry.search_tsvector.op('@@')(tsquery))
            .where(col(entry.status) == DBProceduralStatus(status))
            .order_by(rank.desc())
            .limit(limit)
        )
        if scope is not None:
            stmt = stmt.where(col(entry.scope) == scope)
        if kind is not None:
            stmt = stmt.where(col(entry.kind) == kind)
        if vault_id is not None:
            stmt = stmt.where(col(entry.vault_id) == vault_id)

        async with self._metastore.session() as session:
            rows = (await session.exec(stmt)).all()
        return [(row[0], float(row[1])) for row in rows]

    # ------------------------------------------------------------------
    # Internal: vector search (cosine distance)
    # ------------------------------------------------------------------

    async def _vector_search(
        self,
        query_vec: list[float],
        *,
        scope: ShortLabel | None,
        kind: KindLiteral | None,
        status: str,
        vault_id: UUID | None,
        limit: int,
    ) -> list[tuple[UUID, float]]:
        """Cosine-distance top-k over ``trigger_embedding``.

        The trigger (when_to_use / when_to_apply) is the single vector
        leg of the hybrid search (design §6/§18.7; spike §19.1:
        trigger-only beats full-body embedding 18/20 vs 15/20 top-1).
        ``trigger`` is required at write time, so every published row has
        an embedding. The HNSW index is partial WHERE status='published'
        — see migration 064. ``<=>`` is cosine distance, sorted ascending
        (smaller = closer).
        """
        if not query_vec:
            return []
        entry = DBProceduralEntry
        # pgvector's SQLAlchemy comparator — `<=>` (cosine distance),
        # same idiom as KVEntry.embedding.l2_distance in kv.py.
        embedding_col = entry.__table__.c.trigger_embedding
        distance = embedding_col.cosine_distance(query_vec).label('distance')

        stmt = (
            select(col(entry.id), distance)
            .where(embedding_col.is_not(None))
            .where(col(entry.status) == DBProceduralStatus(status))
            .order_by(distance.asc())
            .limit(limit)
        )
        if scope is not None:
            stmt = stmt.where(col(entry.scope) == scope)
        if kind is not None:
            stmt = stmt.where(col(entry.kind) == kind)
        if vault_id is not None:
            stmt = stmt.where(col(entry.vault_id) == vault_id)

        async with self._metastore.session() as session:
            rows = (await session.exec(stmt)).all()
        return [(row[0], float(row[1])) for row in rows]

    # ------------------------------------------------------------------
    # Internal: pin lookup
    # ------------------------------------------------------------------

    async def _pin_lookup(
        self,
        context_keys: list[ShortLabel],
        *,
        scope: ShortLabel | None,
        kind: KindLiteral | None,
        status: str,
        vault_id: UUID | None,
    ) -> dict[UUID, int]:
        """Return ``{entry_id: position}`` for entries pinned at any of
        the requested contexts. Excludes unpublished entries."""
        if not context_keys:
            return {}
        pin = DBProceduralPin
        entry = DBProceduralEntry

        stmt = (
            select(col(pin.entry_id), func.min(pin.position).label('position'))
            .join(entry, col(entry.id) == col(pin.entry_id))
            .where(col(pin.context_key).in_(list(context_keys)))
            .where(col(entry.status) == DBProceduralStatus(status))
            .group_by(col(pin.entry_id))
        )
        if scope is not None:
            stmt = stmt.where(col(entry.scope) == scope)
        if kind is not None:
            stmt = stmt.where(col(entry.kind) == kind)
        if vault_id is not None:
            stmt = stmt.where(col(entry.vault_id) == vault_id)

        async with self._metastore.session() as session:
            rows = (await session.exec(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    # ------------------------------------------------------------------
    # Internal: async embedder wrapper
    # ------------------------------------------------------------------

    async def embed_trigger(self, trigger: str) -> list[float]:
        """Public alias of :meth:`_embed_text` for the write path.

        The facade calls this to compute ``trigger_embedding`` at write
        time (design §18.7: embeddings are computed by the caller and
        passed in — there is no background backfill). Returns ``[]`` on
        failure so the write can proceed; the row stays reachable via
        the BM25 leg until the next trigger edit re-embeds it.
        """
        return await self._embed_text(trigger)

    async def _embed_text(self, text_str: str) -> list[float]:
        """Compute a single embedding vector for ``text_str``.

        Mirrors the public ``MemexAPI.embed_text`` path: shared semaphore
        + ``_instrument('embed')`` + ``asyncio.to_thread`` +
        ``asyncio.wait_for``. Returns an empty list on any failure so
        the caller can degrade to BM25-only.
        """
        try:
            async with get_embedding_semaphore(), _instrument('embed'):
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._embedding_model.encode, [text_str]),
                    timeout=get_embedding_call_timeout(),
                )
        except Exception:
            logger.exception('procedural embed failed for query=%r', text_str[:80])
            return []
        if result is None or len(result) == 0:
            return []
        return [float(x) for x in result[0]]


__all__ = [
    'ProceduralSearchError',
    'ProceduralSearchService',
    'K_RRF',
]
