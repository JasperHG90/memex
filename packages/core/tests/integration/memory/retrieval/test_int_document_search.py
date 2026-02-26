"""Integration tests for DocumentSearchEngine against a real PostgreSQL database."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    Chunk,
    Document,
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
from memex_core.memory.retrieval.document_search import DocumentSearchEngine
from memex_core.memory.extraction.core import content_hash


@pytest.mark.integration
class TestDocumentSearchEngine:
    """Integration tests for hybrid document search with RRF fusion."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def search_engine(self, embedder):
        return DocumentSearchEngine(embedder=embedder)

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
    ) -> tuple[Document, list[Chunk]]:
        """Create a Document with Chunks and a MemoryUnit (needed for graph traversal)."""
        doc = Document(
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
                document_id=doc.id,
                vault_id=vault_id,
                text=text,
                embedding=embedding,
                chunk_index=idx,
                content_hash=content_hash(text),
            )
            session.add(chunk)
            chunks.append(chunk)

        # Graph strategy requires Entity → UnitEntity → MemoryUnit → Document path,
        # so we also create a MemoryUnit linked to this document.
        unit = MemoryUnit(
            id=uuid4(),
            document_id=doc.id,
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
        """Graph strategy retrieves chunks via Entity → UnitEntity → MemoryUnit → Document → Chunk."""
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

        stmt = select(MemoryUnit).where(col(MemoryUnit.document_id) == doc.id)
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
        # Document about Mars (linked to entity Mars)
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

        stmt = select(MemoryUnit).where(col(MemoryUnit.document_id) == doc_mars.id)
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

        stmt = select(MemoryUnit).where(col(MemoryUnit.document_id) == doc.id)
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
        # Should have multiple snippets (one per matching chunk)
        assert len(doc_result.snippets) >= 2

    async def test_metadata_passthrough(
        self, session: AsyncSession, search_engine, embedder
    ) -> None:
        """Document metadata (including retain_params.note_name) is included in results."""
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

        stmt = select(MemoryUnit).where(col(MemoryUnit.document_id) == doc.id)
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
