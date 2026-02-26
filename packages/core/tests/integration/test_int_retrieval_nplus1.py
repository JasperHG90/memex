import pytest
from memex_core.memory.sql_models import Note, MemoryUnit, FactTypes
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_common.config import GLOBAL_VAULT_ID
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import event


@pytest.mark.asyncio
async def test_retrieval_nplus1(session, metastore):
    # 1. Setup Data
    # Create 20 documents, each with 1 MemoryUnit
    docs = []
    units = []
    for i in range(20):
        doc = Note(id=uuid4(), original_text=f'Doc {i}')
        session.add(doc)
        unit = MemoryUnit(
            text=f'Memory {i}',
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note=doc,
            embedding=[0.1] * 384,  # Dummy
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        docs.append(doc)
        units.append(unit)

    await session.commit()

    # 2. Init Engine
    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    # Return a numpy array or list as expected by engine.
    # Engine does: (await asyncio.to_thread(self.embedder.encode, [request.query]))[0].tolist()
    # So encode should return a list of arrays/lists.
    import numpy as np

    mock_embedder.encode.return_value = [np.array([0.1] * 384)]

    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)

    # 3. Capture Query Count
    query_count = 0

    # Hook into the sync engine underlying the async session
    sync_engine = session.bind.sync_engine

    def count_queries(conn, cursor, statement, parameters, context, executemany):
        nonlocal query_count
        query_count += 1
        # print(f"SQL: {statement}")

    event.listen(sync_engine, 'before_cursor_execute', count_queries)

    try:
        # 4. Search
        req = RetrievalRequest(query='test', limit=20)
        results = await engine.retrieve(session, req)

        # 5. Access related attributes to trigger lazy loads if not eager
        # We simulate what downstream code (e.g., API response mapping) might do
        for u in results:
            if u.note:
                _ = u.note.original_text
            _ = u.unit_entities

    finally:
        event.remove(sync_engine, 'before_cursor_execute', count_queries)

    # 6. Assert
    # With N+1:
    # 1 (RRF)
    # 1 (Hydrate MemoryUnits)
    # 20 (Lazy load Documents)
    # 20 (Lazy load UnitEntities - even if empty, it checks)
    # Total > 40

    # With Fix (selectinload):
    # 1 (RRF)
    # 1 (Hydrate MemoryUnits)
    # 1 (Load Documents for all units)
    # 1 (Load UnitEntities for all units)
    # Total ~4

    print(f'Total Queries: {query_count}')
    assert query_count < 10, f'Too many queries: {query_count}. N+1 detected.'
