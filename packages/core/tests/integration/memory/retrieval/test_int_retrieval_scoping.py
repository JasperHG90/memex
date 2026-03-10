import pytest
import pytest_asyncio
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit, Note
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
class TestRetrievalScoping:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    async def test_private_vault_strict_scoping(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Verify that searching a private vault returns ONLY items from that vault.
        It should NOT return Global items.
        """
        # Create Vault A
        from memex_core.memory.sql_models import Vault

        vault_a_id = uuid4()
        vault_a = Vault(id=vault_a_id, name='Vault A')
        session.add(vault_a)

        # Ensure Global Vault exists
        if not await session.get(Vault, GLOBAL_VAULT_ID):
            session.add(Vault(id=GLOBAL_VAULT_ID, name='Global Vault'))

        doc = Note(id=uuid4(), original_text='Scoping Test')
        session.add(doc)

        query_text = 'Universal Truth'
        embedding = embedder.encode([query_text])[0].tolist()

        # Global Unit
        global_unit = MemoryUnit(
            id=uuid4(),
            text=f'Global: {query_text}',
            embedding=embedding,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(global_unit)

        # Private Unit (Vault A)
        private_unit = MemoryUnit(
            id=uuid4(),
            text=f'Private A: {query_text}',
            embedding=embedding,
            vault_id=vault_a_id,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(private_unit)

        await session.commit()

        # Retrieve with context = [Vault A]
        request_a = RetrievalRequest(query=query_text, limit=10, vault_ids=[vault_a_id])
        results_a, _ = await engine_instance.retrieve(session, request_a)

        ids_a = {r.id for r in results_a}

        # Expect Strict Scoping
        assert private_unit.id in ids_a, 'Vault A unit should be visible'
        assert global_unit.id not in ids_a, (
            'Global unit should NOT be visible in strict Vault A context'
        )

    async def test_multi_vault_scoping(self, session: AsyncSession, engine_instance, embedder):
        """
        Verify that searching multiple vaults ([A, Global]) returns items from ALL requested vaults.
        (Inheritance Pattern)
        """
        from memex_core.memory.sql_models import Vault

        # Create Vaults
        vault_a_id = uuid4()
        vault_b_id = uuid4()
        session.add(Vault(id=vault_a_id, name='Vault A'))
        session.add(Vault(id=vault_b_id, name='Vault B'))
        # Ensure Global
        if not await session.get(Vault, GLOBAL_VAULT_ID):
            session.add(Vault(id=GLOBAL_VAULT_ID, name='Global'))

        doc = Note(id=uuid4(), original_text='Inheritance Test')
        session.add(doc)

        query_text = 'Shared Knowledge'
        embedding = embedder.encode([query_text])[0].tolist()

        # Global Unit
        unit_global = MemoryUnit(
            id=uuid4(),
            text=f'Global: {query_text}',
            embedding=embedding,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(unit_global)

        # Vault A Unit
        unit_a = MemoryUnit(
            id=uuid4(),
            text=f'Vault A: {query_text}',
            embedding=embedding,
            vault_id=vault_a_id,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(unit_a)

        # Vault B Unit (Should be excluded)
        unit_b = MemoryUnit(
            id=uuid4(),
            text=f'Vault B: {query_text}',
            embedding=embedding,
            vault_id=vault_b_id,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(unit_b)

        await session.commit()

        # Retrieve with context = [Vault A, Global]
        request_mixed = RetrievalRequest(
            query=query_text, limit=10, vault_ids=[vault_a_id, GLOBAL_VAULT_ID]
        )
        results_mixed, _ = await engine_instance.retrieve(session, request_mixed)

        ids_mixed = {r.id for r in results_mixed}

        # Expect A and Global, but NOT B
        assert unit_global.id in ids_mixed, 'Global unit should be visible'
        assert unit_a.id in ids_mixed, 'Vault A unit should be visible'
        assert unit_b.id not in ids_mixed, 'Vault B unit should NOT be visible'

    async def test_implicit_none_is_global_search(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Verify that passing vault_ids=None (or empty list) defaults to Global Vault search.
        """
        from memex_core.memory.sql_models import Vault

        doc = Note(id=uuid4(), original_text='None Test')
        session.add(doc)
        embedding = embedder.encode(['Testing'])[0].tolist()

        random_vault_id = uuid4()
        session.add(Vault(id=random_vault_id, name='Random Vault'))

        unit = MemoryUnit(
            id=uuid4(),
            text='Any Vault',
            embedding=embedding,
            vault_id=random_vault_id,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=doc.id,
        )
        session.add(unit)
        await session.commit()

        # Request with vault_ids=None
        request_none = RetrievalRequest(query='Testing', limit=10, vault_ids=None)
        results, _ = await engine_instance.retrieve(session, request_none)

        assert unit.id in [r.id for r in results]

        # Request with vault_ids=[] (Empty List)
        request_empty = RetrievalRequest(query='Testing', limit=10, vault_ids=[])
        results_empty, _ = await engine_instance.retrieve(session, request_empty)

        assert unit.id in [r.id for r in results_empty]
