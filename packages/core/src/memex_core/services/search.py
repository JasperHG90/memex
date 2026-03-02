"""Search service — retrieval, note search, and summarization."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import dspy

from memex_common.schemas import NoteSearchRequest, NoteSearchResult

from memex_core.config import MemexConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.retrieval.document_search import NoteSearchEngine
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

    async def retrieve(self, request: RetrievalRequest) -> list[MemoryUnit]:
        """Retrieve memories and synthesized observations using TEMPR Recall."""
        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def search(
        self,
        query: str,
        limit: int = 10,
        skip_opinion_formation: bool = False,
        vault_ids: list[UUID | str] | None = None,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
        include_stale: bool = False,
        debug: bool = False,
    ) -> list[MemoryUnit]:
        """
        Convenience method for search with reranking.
        Scopes to active vault + attached vaults if vault_ids is not provided.
        """
        vaults = []

        if vault_ids:
            for v in vault_ids:
                vaults.append(await self._vaults.resolve_vault_identifier(str(v)))
        else:
            vaults.append(
                await self._vaults.resolve_vault_identifier(self.config.server.active_vault)
            )
            for av in self.config.server.attached_vaults:
                vaults.append(await self._vaults.resolve_vault_identifier(av))

        request = RetrievalRequest(
            query=query,
            limit=limit,
            vault_ids=vaults,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            debug=debug,
        )

        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def summarize_search_results(self, query: str, texts: list[str]) -> str:
        """Synthesize search results into a concise answer with citations."""
        from memex_core.memory.retrieval.prompts import SearchSummarySignature

        predictor = dspy.Predict(SearchSummarySignature)

        async with self.metastore.session() as session:
            prediction, _ = await run_dspy_operation(
                lm=self.lm,
                predictor=predictor,
                input_kwargs={'query': query, 'search_results': texts},
                session=session,
                context_metadata={'operation': 'search_summary'},
            )
            await session.commit()

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
    ) -> list[NoteSearchResult]:
        """Search for documents containing relevant information using raw chunks."""
        vaults = []
        if vault_ids:
            for v in vault_ids:
                vaults.append(await self._vaults.resolve_vault_identifier(str(v)))
        else:
            vaults.append(
                await self._vaults.resolve_vault_identifier(self.config.server.active_vault)
            )
            for av in self.config.server.attached_vaults:
                vaults.append(await self._vaults.resolve_vault_identifier(av))

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
            return await self._doc_search.search(session, request)

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
