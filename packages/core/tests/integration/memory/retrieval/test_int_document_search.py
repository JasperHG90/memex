"""Integration tests for DocumentSearchEngine against a real PostgreSQL database."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    Chunk,
    Note,
    Entity,
    EntityAlias,
    EntityCooccurrence,
    MemoryUnit,
    UnitEntity,
)
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.schemas import NoteSearchRequest
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.document_search import NoteSearchEngine
from memex_core.memory.extraction.core import content_hash


@pytest.mark.integration
class TestNoteSearchEngine:
    """Integration tests for hybrid document search with RRF fusion."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def search_engine(self, embedder):
        return NoteSearchEngine(embedder=embedder)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _seed_document_with_chunks(
        self,
        session: AsyncSession,
        embedder,
        texts: list[str],
        *,
        vault_id=GLOBAL_VAULT_ID,
        original_text: str = 'Test document',
        doc_metadata: dict | None = None,
    ) -> tuple[Note, list[Chunk]]:
        """Create a Note with Chunks and a MemoryUnit (needed for graph traversal)."""
        doc = Note(
            id=uuid4(),
            original_text=original_text,
            vault_id=vault_id,
            doc_metadata=doc_metadata or {},
        )
        session.add(doc)

        chunks = []
        for idx, text in enumerate(texts):
            embedding = embedder.encode([text])[0].tolist()
            chunk = Chunk(
                id=uuid4(),
                note_id=doc.id,
                vault_id=vault_id,
                text=text,
                embedding=embedding,
                chunk_index=idx,
                content_hash=content_hash(text),
            )
            session.add(chunk)
            chunks.append(chunk)

        # Graph strategy requires Entity → UnitEntity → MemoryUnit → Note path,
        # so we also create a MemoryUnit linked to this document.
        unit = MemoryUnit(
            id=uuid4(),
            note_id=doc.id,
            text=texts[0],
            embedding=embedder.encode([texts[0]])[0].tolist(),
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            vault_id=vault_id,
        )
        session.add(unit)
        await session.flush()

        return doc, chunks

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    async def test_search_empty_db(self, session: AsyncSession, search_engine) -> None:
        """Search against an empty database returns no results."""
        request = NoteSearchRequest(query='Nothing here')
        results = await search_engine.search(session, request)
        assert results == []

    async def test_semantic_search_returns_relevant_document(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Semantic strategy finds documents with chunks close in embedding space."""
        doc, chunks = await self._seed_document_with_chunks(
            session,
            embedder,
            ['Elon Musk founded SpaceX to make life multiplanetary.'],
        )
        await session.commit()

        request = NoteSearchRequest(
            query='SpaceX rocket launches',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        assert len(results) >= 1
        doc_ids = [r.note_id for r in results]
        assert doc.id in doc_ids

    async def test_keyword_search_returns_matching_document(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Keyword (full-text) strategy finds documents containing query terms."""
        doc, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            ['PostgreSQL is a powerful open-source relational database system.'],
        )
        await session.commit()

        request = NoteSearchRequest(
            query='PostgreSQL database',
            strategies=['keyword'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        assert len(results) >= 1
        doc_ids = [r.note_id for r in results]
        assert doc.id in doc_ids

    async def test_graph_first_order_search(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Graph strategy retrieves chunks via Entity → UnitEntity → MemoryUnit → Note → Chunk."""
        doc, chunks = await self._seed_document_with_chunks(
            session,
            embedder,
            ['Tesla reported record quarterly earnings.'],
        )

        # Create an entity and link it to the memory unit
        entity = Entity(id=uuid4(), canonical_name='Tesla')
        session.add(entity)
        await session.flush()

        # Find the MemoryUnit we created for this document
        from sqlmodel import select, col

        stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == doc.id)
        result = await session.exec(stmt)
        unit = result.first()
        assert unit is not None

        ue = UnitEntity(unit_id=unit.id, entity_id=entity.id)
        session.add(ue)
        await session.commit()

        request = NoteSearchRequest(
            query='Tesla',
            strategies=['graph'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        assert len(results) >= 1
        doc_ids = [r.note_id for r in results]
        assert doc.id in doc_ids

    async def test_graph_second_order_via_cooccurrence(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Graph strategy retrieves chunks for co-occurring entities (2nd order)."""
        # Note about Mars (linked to entity Mars)
        doc_mars, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            ['The surface of Mars is covered in iron oxide dust.'],
        )

        e_spacex = Entity(id=uuid4(), canonical_name='SpaceX')
        e_mars = Entity(id=uuid4(), canonical_name='Mars')
        session.add_all([e_spacex, e_mars])
        await session.flush()

        # Link Mars entity to the Mars document's MemoryUnit
        from sqlmodel import select, col

        stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == doc_mars.id)
        result = await session.exec(stmt)
        unit_mars = result.first()
        assert unit_mars is not None

        ue_mars = UnitEntity(unit_id=unit_mars.id, entity_id=e_mars.id)
        session.add(ue_mars)

        # Co-occurrence: SpaceX <-> Mars
        if e_spacex.id < e_mars.id:
            e1, e2 = e_spacex, e_mars
        else:
            e1, e2 = e_mars, e_spacex

        co = EntityCooccurrence(entity_id_1=e1.id, entity_id_2=e2.id, cooccurrence_count=10)
        session.add(co)
        await session.commit()

        # Query for "SpaceX" — should find Mars document via co-occurrence
        request = NoteSearchRequest(
            query='SpaceX',
            strategies=['graph'],
            limit=10,
        )
        results = await search_engine.search(session, request)

        doc_ids = [r.note_id for r in results]
        assert doc_mars.id in doc_ids, (
            'Failed to retrieve document via 2nd-order co-occurrence graph traversal.'
        )

    async def test_hybrid_fusion_all_strategies(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """All three strategies combined via RRF produce results."""
        doc, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            [
                'Python is a versatile programming language.',
                'Python supports multiple programming paradigms.',
            ],
        )

        entity = Entity(id=uuid4(), canonical_name='Python')
        session.add(entity)
        await session.flush()

        from sqlmodel import select, col

        stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == doc.id)
        result = await session.exec(stmt)
        unit = result.first()
        assert unit is not None

        ue = UnitEntity(unit_id=unit.id, entity_id=entity.id)
        session.add(ue)
        await session.commit()

        # All strategies active (default)
        request = NoteSearchRequest(
            query='Python programming language',
            limit=5,
        )
        results = await search_engine.search(session, request)

        assert len(results) >= 1
        doc_ids = [r.note_id for r in results]
        assert doc.id in doc_ids

    async def test_limit_respected(self, session: AsyncSession, search_engine, embedder) -> None:
        """The limit parameter caps the number of returned documents."""
        # Create 5 distinct documents
        for i in range(5):
            await self._seed_document_with_chunks(
                session,
                embedder,
                [f'Machine learning algorithm variant {i} {uuid4().hex[:8]}'],
            )
        await session.commit()

        request = NoteSearchRequest(
            query='machine learning algorithm',
            strategies=['semantic'],
            limit=2,
        )
        results = await search_engine.search(session, request)

        assert len(results) <= 2

    async def test_empty_strategies_returns_empty(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Passing an empty strategy list returns no results."""
        await self._seed_document_with_chunks(
            session, embedder, ['Some text that should not appear.']
        )
        await session.commit()

        request = NoteSearchRequest(
            query='Some text',
            strategies=[],
            limit=5,
        )
        results = await search_engine.search(session, request)
        assert results == []

    async def test_strategy_weights_affect_ranking(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Custom strategy weights change the relative contribution of each strategy."""
        doc, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            ['Artificial intelligence is transforming healthcare diagnostics.'],
        )
        await session.commit()

        # Run with default weights
        request_default = NoteSearchRequest(
            query='AI healthcare',
            strategies=['semantic', 'keyword'],
            limit=5,
        )
        results_default = await search_engine.search(session, request_default)

        # Run with semantic heavily weighted
        request_weighted = NoteSearchRequest(
            query='AI healthcare',
            strategies=['semantic', 'keyword'],
            strategy_weights={'semantic': 5.0, 'keyword': 0.1},
            limit=5,
        )
        results_weighted = await search_engine.search(session, request_weighted)

        # Both should find the document
        assert len(results_default) >= 1
        assert len(results_weighted) >= 1

        default_ids = [r.note_id for r in results_default]
        weighted_ids = [r.note_id for r in results_weighted]
        assert doc.id in default_ids
        assert doc.id in weighted_ids

    async def test_multiple_chunks_grouped_into_snippets(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Multiple chunks from the same document are grouped as snippets."""
        doc, chunks = await self._seed_document_with_chunks(
            session,
            embedder,
            [
                'Chapter 1: The history of quantum computing begins in the 1980s.',
                'Chapter 2: Quantum gates form the basis of quantum circuits.',
                'Chapter 3: Quantum error correction is essential for scalability.',
            ],
        )
        await session.commit()

        request = NoteSearchRequest(
            query='quantum computing circuits gates',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        assert len(results) >= 1
        # Find the result for our document
        doc_result = next((r for r in results if r.note_id == doc.id), None)
        assert doc_result is not None
        # Result should exist (chunks are grouped by document)
        assert doc_result.score > 0

    async def test_metadata_passthrough(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Note metadata (including retain_params.note_name) is included in results."""
        meta = {'source': 'test', 'retain_params': {'note_name': 'My Research Note'}}
        doc, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            ['Specific unique content for metadata passthrough test.'],
            doc_metadata=meta,
        )
        await session.commit()

        request = NoteSearchRequest(
            query='metadata passthrough test unique content',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        doc_result = next((r for r in results if r.note_id == doc.id), None)
        assert doc_result is not None
        assert doc_result.metadata.get('source') == 'test'
        assert doc_result.metadata.get('name') == 'My Research Note'

    async def test_graph_with_entity_alias(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Graph strategy resolves entity aliases to find linked documents."""
        doc, _ = await self._seed_document_with_chunks(
            session,
            embedder,
            ['The Model 3 has become one of the best-selling electric vehicles.'],
        )

        entity = Entity(id=uuid4(), canonical_name='Tesla Motors')
        session.add(entity)
        await session.flush()

        # Add an alias that matches the query
        alias = EntityAlias(canonical_id=entity.id, name='Tesla')
        session.add(alias)

        from sqlmodel import select, col

        stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == doc.id)
        result = await session.exec(stmt)
        unit = result.first()
        assert unit is not None

        ue = UnitEntity(unit_id=unit.id, entity_id=entity.id)
        session.add(ue)
        await session.commit()

        request = NoteSearchRequest(
            query='Tesla',
            strategies=['graph'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        doc_ids = [r.note_id for r in results]
        assert doc.id in doc_ids, (
            'Graph strategy should resolve entity alias "Tesla" → "Tesla Motors".'
        )


@pytest.mark.integration
class TestMMR:
    """Integration tests for MMR (Maximal Marginal Relevance) re-ranking."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def search_engine(self, embedder):
        return NoteSearchEngine(embedder=embedder)

    async def _seed_document(
        self,
        session: AsyncSession,
        embedder,
        text: str,
        *,
        vault_id=GLOBAL_VAULT_ID,
        doc_metadata: dict | None = None,
    ) -> Note:
        """Create a Note with a single Chunk."""
        doc = Note(
            id=uuid4(),
            original_text=text,
            vault_id=vault_id,
            doc_metadata=doc_metadata or {},
        )
        session.add(doc)

        embedding = embedder.encode([text])[0].tolist()
        chunk = Chunk(
            id=uuid4(),
            note_id=doc.id,
            vault_id=vault_id,
            text=text,
            embedding=embedding,
            chunk_index=0,
            content_hash=content_hash(text),
        )
        session.add(chunk)
        await session.flush()
        return doc

    async def test_mmr_promotes_diversity(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """MMR re-ranks results to promote diverse documents."""
        # Create 3 documents about the same topic (similar embeddings)
        doc_a = await self._seed_document(
            session,
            embedder,
            'Machine learning is a subset of artificial intelligence that enables systems to learn from data.',
        )
        doc_b = await self._seed_document(
            session,
            embedder,
            'Machine learning algorithms can identify patterns in large datasets automatically.',
        )
        doc_c = await self._seed_document(
            session,
            embedder,
            'The culinary traditions of France emphasize fresh ingredients and classical techniques.',
        )
        await session.commit()

        # Search with pure relevance (λ=1.0) - should rank by similarity only
        request_relevance = NoteSearchRequest(
            query='machine learning artificial intelligence',
            strategies=['semantic'],
            limit=3,
            mmr_lambda=1.0,
        )
        results_relevance = await search_engine.search(session, request_relevance)

        # All three should be returned with relevance ranking
        assert len(results_relevance) == 3
        relevance_ids = [r.note_id for r in results_relevance]
        assert doc_a.id in relevance_ids
        assert doc_b.id in relevance_ids
        assert doc_c.id in relevance_ids
        # ML docs should be top 2 (higher semantic similarity)
        ml_ids = {doc_a.id, doc_b.id}
        top_two = set(relevance_ids[:2])
        assert ml_ids == top_two, 'ML docs should rank highest with pure relevance'

        # Search with balanced MMR (λ=0.5) - should promote diversity
        request_mmr = NoteSearchRequest(
            query='machine learning artificial intelligence',
            strategies=['semantic'],
            limit=3,
            mmr_lambda=0.5,
        )
        results_mmr = await search_engine.search(session, request_mmr)

        assert len(results_mmr) == 3
        mmr_ids = [r.note_id for r in results_mmr]

        # With MMR, the diverse doc (French cuisine) should rank higher
        # because after picking the first ML doc, the second ML doc is penalized
        # for similarity to the first, while the French cuisine doc is promoted
        mmr_set = set(mmr_ids)
        assert doc_a.id in mmr_set
        assert doc_b.id in mmr_set
        assert doc_c.id in mmr_set

        # The French cuisine doc should not be last (MMR promotes diversity)
        french_position = mmr_ids.index(doc_c.id)
        assert french_position < 2, (
            f'MMR should promote diverse doc; French cuisine was at position {french_position}'
        )

    async def test_mmr_pure_relevance_preserves_order(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """λ=1.0 (pure relevance) preserves semantic ranking order."""
        # Create documents with clearly different relevance
        doc_high = await self._seed_document(
            session,
            embedder,
            'Neural networks are computational models inspired by biological neural networks in the brain.',
        )
        await self._seed_document(
            session,
            embedder,
            'The weather today is sunny with mild temperatures and clear skies.',
        )
        await session.commit()

        request = NoteSearchRequest(
            query='neural networks computational models brain',
            strategies=['semantic'],
            limit=2,
            mmr_lambda=1.0,
        )
        results = await search_engine.search(session, request)

        assert len(results) == 2
        # High-relevance doc should be first regardless of MMR
        assert results[0].note_id == doc_high.id

    async def test_mmr_disabled_by_default(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """When mmr_lambda is None, MMR is not applied."""
        # Create two very similar documents
        doc_a = await self._seed_document(
            session,
            embedder,
            'Deep learning uses neural networks with many layers for feature learning.',
        )
        doc_b = await self._seed_document(
            session,
            embedder,
            'Deep learning applies multi-layer neural networks for representation learning.',
        )
        await session.commit()

        # Search without MMR (default)
        request = NoteSearchRequest(
            query='deep learning neural networks',
            strategies=['semantic'],
            limit=2,
            # mmr_lambda is None by default
        )
        results = await search_engine.search(session, request)

        assert len(results) == 2
        # Both similar docs should be returned (no diversity penalty)
        result_ids = {r.note_id for r in results}
        assert doc_a.id in result_ids
        assert doc_b.id in result_ids

    async def test_mmr_with_many_similar_documents(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """MMR with many similar documents still returns diverse results."""
        # Create 5 similar documents and 1 different one
        similar_texts = [
            'Python is a programming language known for its readable syntax.',
            'Python programming language features dynamic typing and memory management.',
            'The Python language supports multiple programming paradigms.',
            'Python is widely used for web development and data science.',
            'Programming in Python emphasizes code readability and simplicity.',
        ]
        for text in similar_texts:
            await self._seed_document(session, embedder, text)

        doc_diverse = await self._seed_document(
            session,
            embedder,
            'The grand piano has 88 keys and produces sound through vibrating strings.',
        )
        await session.commit()

        # Search with MMR - limit to 3 results
        request = NoteSearchRequest(
            query='Python programming language',
            strategies=['semantic'],
            limit=3,
            mmr_lambda=0.6,
        )
        results = await search_engine.search(session, request)

        assert len(results) == 3
        # The diverse doc (piano) should be included due to MMR diversity bonus
        result_ids = {r.note_id for r in results}
        assert doc_diverse.id in result_ids, (
            'MMR should include diverse document even when many similar docs exist'
        )

    # ------------------------------------------------------------------
    # Block summary tests
    # ------------------------------------------------------------------

    async def test_search_returns_block_summaries_from_chunks(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Chunks with summary JSONB should be returned as BlockSummaryDTO in results."""
        summary_data = {'topic': 'Neural Networks', 'key_points': ['Training', 'Inference']}
        doc = Note(
            id=uuid4(),
            original_text='Neural network research paper.',
            vault_id=GLOBAL_VAULT_ID,
            doc_metadata={},
        )
        session.add(doc)

        chunk_text = 'Neural networks enable deep learning for various tasks.'
        embedding = embedder.encode([chunk_text])[0].tolist()
        chunk = Chunk(
            id=uuid4(),
            note_id=doc.id,
            vault_id=GLOBAL_VAULT_ID,
            text=chunk_text,
            embedding=embedding,
            chunk_index=0,
            content_hash=content_hash(chunk_text),
            summary=summary_data,
            summary_formatted='Neural Networks — Training | Inference',
        )
        session.add(chunk)

        unit = MemoryUnit(
            id=uuid4(),
            note_id=doc.id,
            text=chunk_text,
            embedding=embedding,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        await session.commit()

        request = NoteSearchRequest(
            query='neural networks deep learning',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        matching = [r for r in results if r.note_id == doc.id]
        assert len(matching) == 1
        assert len(matching[0].summaries) == 1
        assert matching[0].summaries[0].topic == 'Neural Networks'
        assert matching[0].summaries[0].key_points == ['Training', 'Inference']

    async def test_search_returns_empty_summaries_for_null_chunks(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Chunks without summary (NULL) should produce empty summaries list."""
        doc = Note(
            id=uuid4(),
            original_text='Old note without summaries.',
            vault_id=GLOBAL_VAULT_ID,
            doc_metadata={},
        )
        session.add(doc)

        chunk_text = 'Legacy content with no block summaries generated.'
        embedding = embedder.encode([chunk_text])[0].tolist()
        chunk = Chunk(
            id=uuid4(),
            note_id=doc.id,
            vault_id=GLOBAL_VAULT_ID,
            text=chunk_text,
            embedding=embedding,
            chunk_index=0,
            content_hash=content_hash(chunk_text),
            # summary=None (default)
        )
        session.add(chunk)

        unit = MemoryUnit(
            id=uuid4(),
            note_id=doc.id,
            text=chunk_text,
            embedding=embedding,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        await session.commit()

        request = NoteSearchRequest(
            query='legacy content block summaries',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        matching = [r for r in results if r.note_id == doc.id]
        assert len(matching) == 1
        assert matching[0].summaries == []

    async def test_search_falls_back_to_page_index_description(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """When no chunk summaries exist, fall back to page_index metadata.description."""
        page_index = {
            'metadata': {'description': 'A guide to distributed systems'},
            'toc': [],
        }
        doc = Note(
            id=uuid4(),
            original_text='Distributed systems guide.',
            vault_id=GLOBAL_VAULT_ID,
            doc_metadata={},
            page_index=page_index,
        )
        session.add(doc)

        chunk_text = 'Distributed systems involve multiple networked computers.'
        embedding = embedder.encode([chunk_text])[0].tolist()
        chunk = Chunk(
            id=uuid4(),
            note_id=doc.id,
            vault_id=GLOBAL_VAULT_ID,
            text=chunk_text,
            embedding=embedding,
            chunk_index=0,
            content_hash=content_hash(chunk_text),
            # summary=None → triggers fallback
        )
        session.add(chunk)

        unit = MemoryUnit(
            id=uuid4(),
            note_id=doc.id,
            text=chunk_text,
            embedding=embedding,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        await session.commit()

        request = NoteSearchRequest(
            query='distributed systems networked',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        matching = [r for r in results if r.note_id == doc.id]
        assert len(matching) == 1
        assert len(matching[0].summaries) == 1
        assert matching[0].summaries[0].topic == 'A guide to distributed systems'
        assert matching[0].summaries[0].key_points == []

    async def test_search_returns_multiple_block_summaries_ordered(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Multiple chunks with summaries should all appear, ordered by chunk_index."""
        doc = Note(
            id=uuid4(),
            original_text='Multi-section paper.',
            vault_id=GLOBAL_VAULT_ID,
            doc_metadata={},
        )
        session.add(doc)

        sections = [
            ('Introduction to machine learning concepts.', 'Introduction', ['ML basics']),
            ('Experimental methodology and data collection.', 'Methods', ['Data pipeline']),
            ('Results showed significant improvement.', 'Results', ['Accuracy improved']),
        ]
        for idx, (text, topic, key_points) in enumerate(sections):
            embedding = embedder.encode([text])[0].tolist()
            chunk = Chunk(
                id=uuid4(),
                note_id=doc.id,
                vault_id=GLOBAL_VAULT_ID,
                text=text,
                embedding=embedding,
                chunk_index=idx,
                content_hash=content_hash(text),
                summary={'topic': topic, 'key_points': key_points},
                summary_formatted=f'{topic} — {" | ".join(key_points)}',
            )
            session.add(chunk)

        unit = MemoryUnit(
            id=uuid4(),
            note_id=doc.id,
            text=sections[0][0],
            embedding=embedder.encode([sections[0][0]])[0].tolist(),
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        await session.commit()

        request = NoteSearchRequest(
            query='machine learning methodology results',
            strategies=['semantic'],
            limit=5,
        )
        results = await search_engine.search(session, request)

        matching = [r for r in results if r.note_id == doc.id]
        assert len(matching) == 1
        assert len(matching[0].summaries) == 3
        assert matching[0].summaries[0].topic == 'Introduction'
        assert matching[0].summaries[1].topic == 'Methods'
        assert matching[0].summaries[2].topic == 'Results'
