"""Document-level search engine using hybrid chunk retrieval with RRF fusion."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

import dspy
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import extract, func, literal, union_all
from sqlalchemy import cast as sql_cast, String
from sqlalchemy.orm import defer
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.models.embedding import FastEmbedder
from memex_core.memory.models.ner import FastNERModel
from memex_core.memory.retrieval.expansion import QueryExpander
from memex_core.memory.retrieval.strategies import DocumentGraphStrategy
from memex_core.memory.sql_models import Chunk, Document, Node
from memex_common.schemas import DocumentSearchRequest, DocumentSearchResult, DocumentSnippet


class RelevantSection(BaseModel):
    """A section identified as relevant by skeleton-tree reasoning."""

    node_id: str = PydanticField(description='The ID of the relevant node from the thin tree.')
    reasoning: str = PydanticField(description='Why this section is relevant to the query.')


class IdentifyRelevantSections(dspy.Signature):
    """Given a user query and a set of document thin trees (table of contents with summaries),
    identify the most relevant sections that would answer the query."""

    query: str = dspy.InputField(desc='The user search query.')
    thin_trees_json: str = dspy.InputField(
        desc='JSON mapping of document_id to its thin tree structure (titles, summaries, node IDs).'
    )
    relevant_sections: list[RelevantSection] = dspy.OutputField(
        desc='The node IDs of the most relevant sections, with reasoning.'
    )


class AnswerFromSections(dspy.Signature):
    """Synthesize a concise, accurate answer to the user query using the retrieved
    document sections. Cite section titles where appropriate."""

    query: str = dspy.InputField(desc='The user search query.')
    sections: str = dspy.InputField(
        desc='The retrieved section texts, each prefixed with its title.'
    )
    answer: str = dspy.OutputField(
        desc='A concise answer to the query, grounded in the provided sections.'
    )


logger = logging.getLogger('memex.core.memory.retrieval.document_search')


@dataclass
class ReasoningOutput:
    """Intermediate result from skeleton-tree section identification."""

    reasoning_by_doc: dict[UUID, list[dict[str, Any]]] = field(default_factory=dict)
    section_texts: list[str] = field(default_factory=list)


K_RRF = 60
CANDIDATE_POOL_SIZE = 60


class DocumentSearchEngine:
    """Searches raw document chunks using hybrid retrieval with weighted RRF fusion.

    Supports three strategies:
    - **semantic**: cosine similarity on chunk embeddings
    - **keyword**: PostgreSQL full-text search (ts_rank_cd)
    - **graph**: entity graph traversal (Entity → MemoryUnit → Document → Chunk)

    Optionally supports multi-query expansion via an LLM-based ``QueryExpander``.
    """

    def __init__(
        self,
        embedder: FastEmbedder,
        ner_model: FastNERModel | None = None,
        lm: dspy.LM | None = None,
    ) -> None:
        self.embedder = embedder
        self.lm = lm
        self.graph_strategy = DocumentGraphStrategy(ner_model=ner_model)
        self.expander = QueryExpander(lm=lm) if lm else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        session: AsyncSession,
        request: DocumentSearchRequest,
    ) -> list[DocumentSearchResult]:
        """Execute a hybrid document search and return grouped results."""
        # 1. Query expansion (multi-query)
        queries = [request.query]
        query_weights = [2.0]

        if request.expand_query and self.expander:
            vault_id = request.vault_ids[0] if request.vault_ids else None
            variations, _ = await self.expander.expand(
                request.query, session=session, vault_id=vault_id
            )
            for var in variations:
                queries.append(var)
                query_weights.append(1.0)

        # 2. Embed all queries
        all_embeddings = await asyncio.to_thread(self.embedder.encode, queries)

        limit = request.limit
        pool_size = max(limit * 2, CANDIDATE_POOL_SIZE)

        # 3. Run search per query and collect scored chunks
        all_chunk_batches: list[tuple[list[Any], float]] = []
        for q, q_emb, q_weight in zip(queries, all_embeddings, query_weights):
            chunk_results = await self._search_single_query(
                session, q, q_emb.tolist(), pool_size, request
            )
            if chunk_results:
                all_chunk_batches.append((chunk_results, q_weight))

        if not all_chunk_batches:
            return []

        # 4. Multi-query RRF fusion
        merged = self._fuse_multi_query(all_chunk_batches, pool_size)

        if not merged:
            return []

        results = await self._group_by_document(session, merged, limit)

        # Skeleton-tree reasoning refinement
        if (request.reason or request.summarize) and self.lm:
            results, reasoning = await self._identify_relevant_sections(
                session, results, request.query, self.lm
            )
            for r in results:
                r.reasoning = reasoning.reasoning_by_doc.get(r.document_id)
            if request.summarize:
                results = await self._synthesize_answer(
                    results, request.query, reasoning.section_texts, self.lm
                )

        return results

    async def _identify_relevant_sections(
        self,
        session: AsyncSession,
        results: list[DocumentSearchResult],
        query: str,
        lm: dspy.LM,
    ) -> tuple[list[DocumentSearchResult], ReasoningOutput]:
        """Identify relevant sections from thin trees via skeleton-tree reasoning.

        For documents that have a ``page_index`` (thin tree):
        1. Use an LLM to identify relevant sections from the thin tree.
        2. Fetch those node texts from the DB.
        3. Replace snippets with the targeted node content.

        Returns:
            Tuple of (updated_results, ReasoningOutput).
        """
        # Collect document IDs that have page_index
        doc_ids = [r.document_id for r in results]
        doc_stmt = (
            select(Document.id, Document.page_index)
            .where(col(Document.id).in_(doc_ids))
            .where(col(Document.page_index).is_not(None))
        )
        doc_results = await session.exec(doc_stmt)
        trees_by_doc: dict[str, Any] = {}
        for doc_id, page_index in doc_results.all():
            if page_index:
                trees_by_doc[str(doc_id)] = page_index

        if not trees_by_doc:
            return results, ReasoningOutput()

        # Step 1: LLM identifies relevant sections from thin trees
        section_predictor = dspy.Predict(IdentifyRelevantSections)
        try:
            with dspy.context(lm=lm):
                prediction = section_predictor(
                    query=query,
                    thin_trees_json=json.dumps(trees_by_doc, default=str),
                )
            relevant_sections = prediction.relevant_sections
            relevant_node_ids = {s.node_id for s in relevant_sections}
        except Exception:
            logger.warning('Skeleton-tree reasoning failed, returning unrefined results.')
            return results, ReasoningOutput()

        if not relevant_node_ids:
            return results, ReasoningOutput()

        # Step 2: Fetch node texts for the identified sections
        node_stmt = (
            select(Node.id, Node.document_id, Node.title, Node.text, Node.level, Node.node_hash)
            .where(col(Node.node_hash).in_(list(relevant_node_ids)))
            .where(col(Node.status) == 'active')
            .order_by(col(Node.seq))
        )
        node_results = await session.exec(node_stmt)
        node_rows = node_results.all()

        if not node_rows:
            return results, ReasoningOutput()

        # Step 3: Build refined snippets and reasoning grouped by document.
        # Map node_hash → (db_node_uuid, document_id) for enriching reasoning.
        hash_to_info: dict[str, tuple[UUID, UUID]] = {}
        doc_snippets: dict[UUID, list[DocumentSnippet]] = {}
        section_texts: list[str] = []
        for node_id, doc_id, title, text, level, node_hash in node_rows:
            hash_to_info[node_hash] = (node_id, doc_id)
            snippet = DocumentSnippet(
                text=text or '',
                score=1.0,
                node_id=node_id,
                node_title=title,
                node_level=level,
            )
            doc_snippets.setdefault(doc_id, []).append(snippet)
            section_texts.append(f'## {title}\n{text}')

        # Build per-document reasoning enriched with the real node UUID.
        reasoning_by_doc: dict[UUID, list[dict[str, Any]]] = {}
        for section in relevant_sections:
            data = section.model_dump()
            info = hash_to_info.get(section.node_id)
            if info:
                node_uuid, doc_id = info
                data['node_uuid'] = str(node_uuid)
                reasoning_by_doc.setdefault(doc_id, []).append(data)

        for result in results:
            if result.document_id in doc_snippets:
                result.snippets = doc_snippets[result.document_id]

        return results, ReasoningOutput(
            reasoning_by_doc=reasoning_by_doc, section_texts=section_texts
        )

    async def _synthesize_answer(
        self,
        results: list[DocumentSearchResult],
        query: str,
        all_section_texts: list[str],
        lm: dspy.LM,
    ) -> list[DocumentSearchResult]:
        """Synthesize an answer from identified sections.

        Runs ``AnswerFromSections`` and attaches the answer to the top result.
        """
        if not all_section_texts or not results:
            return results

        try:
            answer_predictor = dspy.Predict(AnswerFromSections)
            with dspy.context(lm=lm):
                answer_pred = answer_predictor(
                    query=query,
                    sections='\n\n'.join(all_section_texts),
                )
            results[0].answer = answer_pred.answer
        except Exception:
            logger.warning('Answer synthesis failed, returning results without answer.')

        return results

    async def _search_single_query(
        self,
        session: AsyncSession,
        query: str,
        query_embedding: list[float],
        pool_size: int,
        request: DocumentSearchRequest,
    ) -> list[Any]:
        """Run all active strategies for a single query and fuse via RRF."""
        active = set(request.strategies)
        weights = request.strategy_weights or {}

        cte_selects = []

        if 'semantic' in active:
            cte_selects.append(self._semantic_cte(query_embedding, pool_size, request, weights))

        if 'keyword' in active:
            cte_selects.append(self._keyword_cte(query, pool_size, request, weights))

        if 'graph' in active:
            cte_selects.append(self._graph_cte(query, query_embedding, pool_size, request, weights))

        if 'temporal' in active:
            cte_selects.append(self._temporal_cte(pool_size, request, weights))

        if not cte_selects:
            return []

        return await self._fuse_and_fetch(session, cte_selects, pool_size)

    # ------------------------------------------------------------------
    # Strategy CTEs
    # ------------------------------------------------------------------

    def _semantic_cte(
        self,
        query_embedding: list[float],
        pool_size: int,
        request: DocumentSearchRequest,
        weights: dict[str, float],
    ) -> Any:
        weight = weights.get('semantic', 1.0)
        distance = cast(Any, col(Chunk.embedding)).cosine_distance(query_embedding)

        stmt = select(
            Chunk.id,
            func.rank().over(order_by=distance.asc()).label('rnk'),
            literal(weight).label('weight'),
        ).select_from(Chunk)

        if request.vault_ids:
            stmt = stmt.where(col(Chunk.vault_id).in_(request.vault_ids))

        cte = stmt.limit(pool_size).cte('chunk_semantic')
        return select(cte.c.id, cte.c.rnk, cte.c.weight)

    def _keyword_cte(
        self,
        query: str,
        pool_size: int,
        request: DocumentSearchRequest,
        weights: dict[str, float],
    ) -> Any:
        weight = weights.get('keyword', 1.0)

        ts_query_base = func.plainto_tsquery('english', query)
        permissive_query_str = func.regexp_replace(sql_cast(ts_query_base, String), '&', '|', 'g')
        ts_query = func.to_tsquery('english', permissive_query_str)

        # Search on nodes.text and map to block IDs (chunks) via nodes.block_id
        ts_vector_node = func.to_tsvector('english', Node.text)
        rank_node = func.ts_rank_cd(ts_vector_node, ts_query)

        node_stmt = (
            select(
                Node.block_id.label('id'),  # type: ignore[attr-defined]
                func.rank().over(order_by=rank_node.desc()).label('rnk'),
                literal(weight).label('weight'),
            )
            .select_from(Node)
            .where(ts_vector_node.op('@@')(ts_query))
            .where(col(Node.block_id).is_not(None))
            .where(col(Node.status) == 'active')
        )

        if request.vault_ids:
            node_stmt = node_stmt.where(col(Node.vault_id).in_(request.vault_ids))

        # Also search on chunks.text for backward compat (simple strategy docs)
        ts_vector_chunk = func.to_tsvector('english', Chunk.text)
        rank_chunk = func.ts_rank_cd(ts_vector_chunk, ts_query)

        chunk_stmt = (
            select(
                Chunk.id,
                func.rank().over(order_by=rank_chunk.desc()).label('rnk'),
                literal(weight).label('weight'),
            )
            .select_from(Chunk)
            .where(ts_vector_chunk.op('@@')(ts_query))
        )

        if request.vault_ids:
            chunk_stmt = chunk_stmt.where(col(Chunk.vault_id).in_(request.vault_ids))

        # Union both sources, take top results
        combined = union_all(node_stmt.limit(pool_size), chunk_stmt.limit(pool_size))
        cte = combined.cte('chunk_keyword')
        return select(cte.c.id, cte.c.rnk, cte.c.weight)

    def _graph_cte(
        self,
        query: str,
        query_embedding: list[float],
        pool_size: int,
        request: DocumentSearchRequest,
        weights: dict[str, float],
    ) -> Any:
        weight = weights.get('graph', 1.0)
        filters: dict[str, Any] = {}
        if request.vault_ids:
            filters['vault_ids'] = request.vault_ids

        graph_stmt = self.graph_strategy.get_statement(
            query, query_embedding, limit=pool_size, **filters
        )
        subq = graph_stmt.subquery(name='sq_doc_graph')

        cte = (
            select(
                subq.c.id,
                func.rank().over(order_by=subq.c.score.desc()).label('rnk'),
                literal(weight).label('weight'),
            )
            .select_from(subq)
            .cte('chunk_graph')
        )
        return select(cte.c.id, cte.c.rnk, cte.c.weight)

    def _temporal_cte(
        self,
        pool_size: int,
        request: DocumentSearchRequest,
        weights: dict[str, float],
    ) -> Any:
        """Rank chunks by their parent document's publish_date (most recent first)."""
        weight = weights.get('temporal', 0.5)

        # Join Chunk → Document to access publish_date
        epoch = extract('epoch', col(Document.publish_date))

        stmt = (
            select(
                Chunk.id,
                func.rank().over(order_by=epoch.desc()).label('rnk'),
                literal(weight).label('weight'),
            )
            .select_from(Chunk)
            .join(Document, col(Chunk.document_id) == col(Document.id))
            .where(col(Document.publish_date).is_not(None))
        )

        if request.vault_ids:
            stmt = stmt.where(col(Chunk.vault_id).in_(request.vault_ids))

        cte = stmt.limit(pool_size).cte('chunk_temporal')
        return select(cte.c.id, cte.c.rnk, cte.c.weight)

    # ------------------------------------------------------------------
    # Fusion + hydration
    # ------------------------------------------------------------------

    async def _fuse_and_fetch(
        self,
        session: AsyncSession,
        cte_selects: list[Any],
        pool_size: int,
    ) -> list[Any]:
        union_query = union_all(*cte_selects)
        candidates_cte = union_query.cte('chunk_candidates')

        rrf_score = func.sum(candidates_cte.c.weight / (K_RRF + candidates_cte.c.rnk)).label(
            'rrf_score'
        )

        scores_cte = (
            select(candidates_cte.c.id, rrf_score)
            .select_from(candidates_cte)
            .group_by(candidates_cte.c.id)
        ).cte('chunk_scores')

        final_stmt = (
            select(Chunk, scores_cte.c.rrf_score)
            .join(scores_cte, col(Chunk.id) == scores_cte.c.id)
            .options(defer(Chunk.embedding))  # type: ignore
            .order_by(scores_cte.c.rrf_score.desc())
            .limit(pool_size)
        )

        result = await session.exec(final_stmt)
        return list(result.all())

    async def _group_by_document(
        self,
        session: AsyncSession,
        chunk_results: list[Any],
        limit: int,
    ) -> list[DocumentSearchResult]:
        # Group chunks by document
        doc_to_chunks: dict[UUID, list[tuple[Chunk, float]]] = {}
        for chunk, score in chunk_results:
            doc_to_chunks.setdefault(chunk.document_id, []).append((chunk, score))

        # Fetch documents
        doc_ids = list(doc_to_chunks.keys())
        doc_stmt = select(Document).where(col(Document.id).in_(doc_ids))
        docs_result = await session.exec(doc_stmt)
        docs = {d.id: d for d in docs_result.all()}

        # For blocks without text (page_index strategy), fetch node texts
        block_ids_needing_text: list[UUID] = []
        for chunks_with_scores in doc_to_chunks.values():
            for c, _ in chunks_with_scores:
                if not c.text or not c.text.strip():
                    block_ids_needing_text.append(c.id)

        node_texts_by_block: dict[UUID, str] = {}
        if block_ids_needing_text:
            node_stmt = (
                select(Node.block_id, Node.text)
                .where(
                    col(Node.block_id).in_(block_ids_needing_text),
                    col(Node.status) == 'active',
                )
                .order_by(col(Node.seq))
            )
            node_results = await session.exec(node_stmt)
            for block_id, text in node_results.all():
                existing = node_texts_by_block.get(block_id, '')
                node_texts_by_block[block_id] = f'{existing}\n\n{text}' if existing else text

        # Build results
        final_results: list[DocumentSearchResult] = []
        for doc_id, chunks_with_scores in doc_to_chunks.items():
            if doc_id not in docs:
                continue

            doc = docs[doc_id]
            best_score = max(s for _, s in chunks_with_scores)

            snippets = sorted(
                [
                    DocumentSnippet(
                        text=c.text
                        if c.text and c.text.strip()
                        else node_texts_by_block.get(c.id, ''),
                        score=s,
                        chunk_index=c.chunk_index,
                    )
                    for c, s in chunks_with_scores
                ],
                key=lambda x: x.score,
                reverse=True,
            )

            metadata = dict(doc.doc_metadata)
            retain_params = metadata.get('retain_params', {})
            if isinstance(retain_params, dict):
                note_name = retain_params.get('note_name')
                if note_name:
                    metadata['name'] = note_name
                    metadata['title'] = note_name

            final_results.append(
                DocumentSearchResult(
                    document_id=doc.id, metadata=metadata, snippets=snippets, score=best_score
                )
            )

        final_results.sort(key=lambda x: x.score, reverse=True)
        return final_results[:limit]

    # ------------------------------------------------------------------
    # Multi-query fusion
    # ------------------------------------------------------------------

    @staticmethod
    def _fuse_multi_query(
        batches: list[tuple[list[Any], float]], limit: int
    ) -> list[tuple[Any, float]]:
        """Fuse chunk results from multiple query variants using weighted RRF.

        Each batch item is a list of ``(Chunk, rrf_score)`` tuples paired with a
        query-level weight.  Returns a deduplicated, re-scored list of
        ``(Chunk, fused_score)`` ordered by descending score.
        """
        if len(batches) == 1:
            return list(batches[0][0])

        scores: dict[UUID, float] = {}
        chunks: dict[UUID, Any] = {}  # keep first seen Chunk object
        for batch, batch_weight in batches:
            for rank, (chunk, _score) in enumerate(batch):
                rrf = batch_weight / (K_RRF + rank + 1)
                scores[chunk.id] = scores.get(chunk.id, 0.0) + rrf
                if chunk.id not in chunks:
                    chunks[chunk.id] = chunk

        sorted_ids = sorted(scores, key=lambda k: scores[k], reverse=True)[:limit]
        return [(chunks[cid], scores[cid]) for cid in sorted_ids]
