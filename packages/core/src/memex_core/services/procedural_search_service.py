"""V7 Procedural Plane — search service.

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
mirroring the public ``MemexAPI.embed_text`` path. Search-time
embeddings are the only time the embedder is called from this module;
the repository does not embed on write (lazy embedding, see V7 §3.4).

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
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import col, select

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
                    score=scores[eid],
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
                        score=scores[entry.id],
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
                .order_by(
                    col(DBProceduralPin.context_key).asc(),
                    col(DBProceduralPin.position).asc(),
                )
            )
            if scope is not None:
                stmt = stmt.where(col(DBProceduralEntry.scope) == scope)
            if vault_id is not None:
                stmt = stmt.where(col(DBProceduralEntry.vault_id) == vault_id)

            results = (await session.exec(stmt)).all()

            # Group by context_key; respect the per-context cap.
            per_context_count: dict[str, int] = {ck: 0 for ck in context_keys}
            for pin, entry in results:
                if per_context_count.get(pin.context_key, 0) >= limit_per_context:
                    continue
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
        # NB: each filter parameter is referenced twice (once for the
        # NULL check, once for the value comparison). asyncpg refuses
        # to infer the type for an ambiguous parameter, so cast each
        # occurrence explicitly via ``CAST(:x AS ...)`` rather than
        # relying on context-driven inference.
        sql = text(
            """
            SELECT
                id,
                ts_rank_cd(
                    search_tsvector,
                    plainto_tsquery('english'::regconfig, CAST(:q AS text))
                ) AS rank
            FROM procedural_entries
            WHERE search_tsvector @@ plainto_tsquery('english'::regconfig, CAST(:q AS text))
              AND status = CAST(:status AS varchar)
              AND (CAST(:scope AS text) IS NULL OR scope = CAST(:scope AS text))
              AND (CAST(:kind AS varchar) IS NULL OR kind = CAST(:kind AS varchar))
              AND (CAST(:vault_id AS uuid) IS NULL OR vault_id = CAST(:vault_id AS uuid))
            ORDER BY rank DESC
            LIMIT CAST(:limit AS int)
            """
        )
        async with self._metastore.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        'q': query,
                        'status': status,
                        'scope': scope,
                        'kind': kind,
                        'vault_id': vault_id,
                        'limit': limit,
                    },
                )
            ).all()
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
        Rows without a trigger (legacy kv_backfill) are reachable via
        the BM25 leg only. The HNSW index is partial WHERE
        status='published' — see migration 064. ``<=>`` is cosine
        distance, sorted ascending (smaller = closer).
        """
        if not query_vec:
            return []
        # pgvector accepts the vector as a string '[v1,v2,...]'.
        vec_literal = '[' + ','.join(f'{x:.7f}' for x in query_vec) + ']'

        # Explicit CAST on every reused parameter — see the BM25
        # helper for why asyncpg refuses ambiguous-type params.
        sql = text(
            """
            SELECT
                id,
                trigger_embedding <=> CAST(:vec AS vector) AS distance
            FROM procedural_entries
            WHERE trigger_embedding IS NOT NULL
              AND status = CAST(:status AS varchar)
              AND (CAST(:scope AS text) IS NULL OR scope = CAST(:scope AS text))
              AND (CAST(:kind AS varchar) IS NULL OR kind = CAST(:kind AS varchar))
              AND (CAST(:vault_id AS uuid) IS NULL OR vault_id = CAST(:vault_id AS uuid))
            ORDER BY trigger_embedding <=> CAST(:vec AS vector)
            LIMIT CAST(:limit AS int)
            """
        )
        params: dict[str, Any] = {
            'vec': vec_literal,
            'status': status,
            'scope': scope,
            'kind': kind,
            'vault_id': vault_id,
            'limit': limit,
        }

        async with self._metastore.session() as session:
            rows = (await session.execute(sql, params)).all()
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
        sql = text(
            """
            SELECT p.entry_id, MIN(p.position) AS position
            FROM procedural_pins p
            JOIN procedural_entries e ON e.id = p.entry_id
            WHERE p.context_key = ANY(:contexts)
              AND e.status = CAST(:status AS varchar)
              AND (CAST(:scope AS text) IS NULL OR e.scope = CAST(:scope AS text))
              AND (CAST(:kind AS varchar) IS NULL OR e.kind = CAST(:kind AS varchar))
              AND (CAST(:vault_id AS uuid) IS NULL OR e.vault_id = CAST(:vault_id AS uuid))
            GROUP BY p.entry_id
            """
        )
        async with self._metastore.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        'contexts': list(context_keys),
                        'status': status,
                        'scope': scope,
                        'kind': kind,
                        'vault_id': vault_id,
                    },
                )
            ).all()
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
