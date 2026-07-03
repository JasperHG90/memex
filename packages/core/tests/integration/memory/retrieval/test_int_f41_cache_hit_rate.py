"""F41 — cross-encoder score cache integration test.

Smoke-validates that two identical retrieval calls against the real Postgres
substrate + real reranker produce a cache hit on the second pass.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import MemoryUnit, Note
from memex_core.metrics import (
    CROSS_ENCODER_CACHE_HITS_TOTAL,
    CROSS_ENCODER_CACHE_MISSES_TOTAL,
)


from _metric_helpers import read_counter_total as _read_metric  # noqa: E402


@pytest.mark.integration
class TestF41CacheHitRate:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='class')
    async def reranker(self):
        return await get_reranking_model()

    @pytest_asyncio.fixture(scope='function')
    async def engine_instance(self, embedder, reranker):
        return RetrievalEngine(
            embedder=embedder,
            reranker=reranker,
            retrieval_config=RetrievalConfig(cross_encoder_cache_enabled=True),
        )

    async def _seed(self, session: AsyncSession, embedder) -> None:
        doc = Note(id=uuid4(), original_text='F41 cache test corpus')
        session.add(doc)

        texts = [
            'Project Chimera launched in Q3 with the new dashboard UI.',
            'The redis cache stampede caused the production outage on Tuesday.',
            'Quarterly review highlighted the need for better observability.',
            'Engineering team migrated from Postgres 14 to Postgres 18 last month.',
        ]
        for text in texts:
            embedding = embedder.encode([text])[0].tolist()
            unit = MemoryUnit(
                id=uuid4(),
                text=text,
                embedding=embedding,
                fact_type=FactTypes.WORLD,
                event_date=datetime.now(timezone.utc),
                note_id=doc.id,
            )
            session.add(unit)
        await session.commit()

    async def test_repeated_query_produces_cache_hits(
        self, session: AsyncSession, engine_instance, embedder
    ):
        await self._seed(session, embedder)

        hits_before = _read_metric(CROSS_ENCODER_CACHE_HITS_TOTAL)
        misses_before = _read_metric(CROSS_ENCODER_CACHE_MISSES_TOTAL)

        query = 'What caused the production outage related to redis?'

        first, _ = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=4))
        misses_after_first = _read_metric(CROSS_ENCODER_CACHE_MISSES_TOTAL)
        assert misses_after_first > misses_before, 'first pass should record misses'

        second, _ = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=4))

        hits_after = _read_metric(CROSS_ENCODER_CACHE_HITS_TOTAL)
        misses_after_second = _read_metric(CROSS_ENCODER_CACHE_MISSES_TOTAL)

        assert hits_after > hits_before, 'second identical query should hit the cache'
        assert misses_after_second == misses_after_first, (
            'second pass should not produce additional misses for already-seen units'
        )
        assert len(first) == len(second), 'result set size should be stable'
