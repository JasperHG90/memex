"""Search service — retrieval, note search, and summarization."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from uuid import UUID

import dspy

from memex_common.schemas import (
    NoteSearchRequest,
    NoteSearchResult,
    SurveyFact,
    SurveyResponse,
    SurveyTopic,
)

from memex_core.config import MemexConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.retrieval.document_search import NoteSearchEngine
from memex_core.memory.retrieval.expansion import SurveyDecomposer
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.vaults import VaultService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.search')


class SearchService:
    """Memory retrieval, note search, and result summarization."""

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        config: MemexConfig,
        lm: dspy.LM,
        memory: MemoryEngine,
        doc_search: NoteSearchEngine,
        vaults: VaultService,
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.lm = lm
        self.memory = memory
        self._doc_search = doc_search
        self._vaults = vaults

    async def retrieve(self, request: RetrievalRequest) -> tuple[list[MemoryUnit], Any]:
        """Retrieve memories and synthesized observations using TEMPR Recall."""
        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def search(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID | str] | None = None,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
        include_stale: bool = False,
        include_superseded: bool = False,
        debug: bool = False,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        tags: list[str] | None = None,
        source_context: str | None = None,
        reference_date: dt.datetime | None = None,
    ) -> tuple[list[MemoryUnit], Any]:
        """
        Convenience method for search with reranking.
        Scopes to default reader vault if vault_ids is not provided.
        """
        from memex_common.vault_utils import ALL_VAULTS_WILDCARD

        vaults = []

        if vault_ids and ALL_VAULTS_WILDCARD in [str(v) for v in vault_ids]:
            all_v = await self._vaults.list_vaults()
            vaults = [v.id for v in all_v]
        elif vault_ids:
            for v in vault_ids:
                vaults.append(await self._vaults.resolve_vault_identifier(str(v)))
        else:
            vaults.append(
                await self._vaults.resolve_vault_identifier(self.config.server.default_reader_vault)
            )

        request = RetrievalRequest(
            query=query,
            limit=limit,
            vault_ids=vaults,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            include_superseded=include_superseded,
            debug=debug,
            after=after,
            before=before,
            tags=tags,
            source_context=source_context,
            reference_date=reference_date,
        )

        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def summarize_search_results(self, query: str, texts: list[str]) -> str:
        """Synthesize search results into a concise answer with citations."""
        from memex_core.memory.retrieval.prompts import SearchSummarySignature

        predictor = dspy.Predict(SearchSummarySignature)

        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={'query': query, 'search_results': texts},
            operation_name='search',
        )

        return prediction.summary

    async def search_notes(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID | str] | None = None,
        expand_query: bool = False,
        fusion_strategy: str = 'rrf',
        strategies: list[str] | None = None,
        strategy_weights: dict[str, float] | None = None,
        reason: bool = False,
        summarize: bool = False,
        mmr_lambda: float | None = None,
        reference_date: dt.datetime | None = None,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        tags: list[str] | None = None,
    ) -> list[NoteSearchResult]:
        """Search for documents containing relevant information using raw chunks."""
        from memex_common.vault_utils import ALL_VAULTS_WILDCARD

        vaults = []
        if vault_ids and ALL_VAULTS_WILDCARD in [str(v) for v in vault_ids]:
            all_v = await self._vaults.list_vaults()
            vaults = [v.id for v in all_v]
        elif vault_ids:
            for v in vault_ids:
                vaults.append(await self._vaults.resolve_vault_identifier(str(v)))
        else:
            vaults.append(
                await self._vaults.resolve_vault_identifier(self.config.server.default_reader_vault)
            )

        kwargs: dict[str, Any] = {}
        if strategies is not None:
            kwargs['strategies'] = strategies
        if strategy_weights is not None:
            kwargs['strategy_weights'] = strategy_weights

        # Resolve effective mmr_lambda: per-request override, else config default
        effective_mmr_lambda = mmr_lambda
        if effective_mmr_lambda is None:
            effective_mmr_lambda = self.config.server.document.mmr_lambda
        if effective_mmr_lambda is not None:
            kwargs['mmr_lambda'] = effective_mmr_lambda

        if after is not None:
            kwargs['after'] = after
        if before is not None:
            kwargs['before'] = before
        if tags is not None:
            kwargs['tags'] = tags
        if reference_date is not None:
            kwargs['reference_date'] = reference_date

        request = NoteSearchRequest(
            query=query,
            limit=limit,
            vault_ids=vaults,
            expand_query=expand_query,
            fusion_strategy=fusion_strategy,
            reason=reason,
            summarize=summarize,
            **kwargs,
        )

        async with self.metastore.session() as session:
            results = await self._doc_search.search(session, request)

        # Enrich results with vault info (#7)
        if results:
            vault_names = await self._resolve_vault_names(vaults)
            for r in results:
                vid = r.metadata.get('vault_id')
                if vid:
                    r.vault_id = UUID(vid) if isinstance(vid, str) else vid
                    r.vault_name = vault_names.get(r.vault_id)

        return results

    async def _resolve_vault_names(self, vault_ids: list[UUID]) -> dict[UUID, str]:
        """Batch-resolve vault names for a list of vault IDs."""
        from memex_core.memory.sql_models import Vault

        result: dict[UUID, str] = {}
        async with self.metastore.session() as session:
            for vid in vault_ids:
                vault = await session.get(Vault, vid)
                if vault:
                    result[vid] = vault.name
        return result

    async def resolve_source_notes(self, unit_ids: list[UUID]) -> dict[UUID, UUID]:
        """
        Resolve the source note ID for a list of Memory Unit IDs.
        Returns a map of {unit_id: note_id}.
        """
        from sqlmodel import select

        if not unit_ids:
            return {}

        async with self.metastore.session() as session:
            from sqlmodel import col

            stmt = select(MemoryUnit.id, MemoryUnit.note_id).where(col(MemoryUnit.id).in_(unit_ids))
            results = (await session.exec(stmt)).all()

            return {row[0]: row[1] for row in results if row[1] is not None}

    async def survey(
        self,
        query: str,
        vault_ids: list[UUID] | None = None,
        limit_per_query: int = 10,
        token_budget: int | None = None,
    ) -> SurveyResponse:
        """
        Broad topic survey: decompose into sub-questions, parallel search,
        dedup by memory unit ID, group by note.
        """
        # Resolve vaults
        if vault_ids is None:
            vault_ids = [
                await self._vaults.resolve_vault_identifier(self.config.server.default_reader_vault)
            ]

        # Decompose query into sub-questions
        decomposer = SurveyDecomposer(self.lm)
        sub_queries = await decomposer.decompose(query)

        # Parallel search per sub-question
        async def _search_one(sub_query: str) -> list[MemoryUnit]:
            request = RetrievalRequest(
                query=sub_query,
                limit=limit_per_query,
                vault_ids=vault_ids,
            )
            async with self.metastore.session() as session:
                units, _ = await self.memory.recall(session, request)
            return units

        results_per_query = await asyncio.gather(*[_search_one(sq) for sq in sub_queries])

        # Dedup by memory unit ID, keeping highest-score occurrence
        seen_ids: dict[UUID, MemoryUnit] = {}
        for units in results_per_query:
            for unit in units:
                existing = seen_ids.get(unit.id)
                if existing is None:
                    seen_ids[unit.id] = unit
                else:
                    # Keep the one with the higher score
                    existing_score = getattr(existing, 'score', None) or 0.0
                    new_score = getattr(unit, 'score', None) or 0.0
                    if new_score > existing_score:
                        seen_ids[unit.id] = unit

        all_units = list(seen_ids.values())

        # Sort by score descending
        all_units.sort(key=lambda u: getattr(u, 'score', None) or 0.0, reverse=True)

        # Token budget truncation
        truncated = False
        if token_budget is not None:
            budget_units: list[MemoryUnit] = []
            token_count = 0
            for unit in all_units:
                # Rough token estimate: ~4 chars per token
                unit_tokens = len(unit.text) // 4
                if token_count + unit_tokens > token_budget:
                    truncated = True
                    break
                budget_units.append(unit)
                token_count += unit_tokens
            all_units = budget_units

        # Group by note_id
        note_groups: dict[UUID, list[MemoryUnit]] = {}
        for unit in all_units:
            if unit.note_id is not None:
                note_groups.setdefault(unit.note_id, []).append(unit)

        # Resolve note titles
        note_titles: dict[UUID, str] = {}
        if note_groups:
            from memex_core.memory.sql_models import Note
            from sqlmodel import select, col

            async with self.metastore.session() as session:
                stmt = select(Note.id, Note.title).where(col(Note.id).in_(list(note_groups.keys())))
                rows = (await session.exec(stmt)).all()
                for row in rows:
                    if row[1]:
                        note_titles[row[0]] = row[1]

        # Build topics
        topics: list[SurveyTopic] = []
        for note_id, units in note_groups.items():
            facts = [
                SurveyFact(
                    id=u.id,
                    text=u.text,
                    fact_type=u.fact_type,
                    score=getattr(u, 'score', None),
                )
                for u in units
            ]
            topics.append(
                SurveyTopic(
                    note_id=note_id,
                    title=note_titles.get(note_id),
                    fact_count=len(facts),
                    facts=facts,
                )
            )

        # Sort topics by total fact score descending
        topics.sort(
            key=lambda t: sum(f.score or 0.0 for f in t.facts),
            reverse=True,
        )

        total_facts = sum(t.fact_count for t in topics)

        return SurveyResponse(
            query=query,
            sub_queries=sub_queries,
            topics=topics,
            total_notes=len(topics),
            total_facts=total_facts,
            truncated=truncated,
        )
