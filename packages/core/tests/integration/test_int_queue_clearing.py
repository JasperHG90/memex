import pytest
import uuid
from memex_core.memory.sql_models import (
    Entity,
    ReflectionQueue,
    ReflectionStatus,
    Vault,
)
from memex_common.config import GLOBAL_VAULT_ID
from memex_core.api import MemexAPI
from memex_core.memory.reflect.models import ReflectionRequest
from sqlmodel import select, col


@pytest.mark.asyncio
async def test_cli_queue_clearing_bug(
    metastore,
    filestore,
    memex_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
):
    """
    Reproduce the bug where the CLI 'memex memory reflect' command drops vault_id info,
    causing the queue service to fail to clear items for non-global vaults.
    """
    api = MemexAPI(
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
    )
    await api.initialize()

    async with metastore.session() as session:
        # 1. Setup Data
        # Create a Specific Vault
        vault_id = uuid.uuid4()
        vault = Vault(id=vault_id, name='specific_vault')
        session.add(vault)

        # Create an Entity
        entity_id = uuid.uuid4()
        entity = Entity(id=entity_id, canonical_name='Test Entity CLI Bug')
        session.add(entity)

        # Add to Queue for SPECIFIC VAULT
        queue_item = ReflectionQueue(
            entity_id=entity_id,
            vault_id=vault_id,  # <--- Specific Vault
            status=ReflectionStatus.PENDING,
            priority_score=10.0,
        )
        session.add(queue_item)
        await session.commit()
        await session.refresh(queue_item)

        # 2. Simulate CLI Behavior    # The CLI fetches items:
    # queue_items = await api.get_reflection_queue_batch(limit=limit)
    # entities_to_process = [q.entity_id for q in queue_items]  <-- LOSS OF DATA

    queue_items = await api.get_reflection_queue_batch(limit=10)
    assert len(queue_items) == 1

    # CLI Logic: Create requests with ONLY entity_id (defaults to GLOBAL_VAULT_ID)
    entities_to_process = [q.entity_id for q in queue_items]
    requests = [ReflectionRequest(entity_id=eid) for eid in entities_to_process]

    assert requests[0].vault_id == GLOBAL_VAULT_ID  # Default behavior

    # Mock reflect_batch in API or Engine to return a result that mimics what happens
    # When reflect_batch runs with GLOBAL_VAULT_ID, it produces a MentalModel for GLOBAL_VAULT_ID.

    from memex_core.memory.reflect.reflection import ReflectionEngine
    from memex_core.memory.reflect.models import MentalModel

    async def mock_reflect_batch(reqs):
        results = []
        for req in reqs:
            # It returns a MentalModel matching the request's vault_id
            mm = MentalModel(
                id=uuid.uuid4(),
                entity_id=req.entity_id,
                vault_id=req.vault_id,  # This will be GLOBAL if CLI passed GLOBAL
                name='Reflected Entity',
            )
            results.append(mm)
        return results

    from unittest.mock import patch

    with patch.object(ReflectionEngine, 'reflect_batch', side_effect=mock_reflect_batch):
        # 3. Simulate Buggy CLI: Reflect with GLOBAL_VAULT_ID
        # (requests already has GLOBAL_VAULT_ID from earlier in the test)
        await api.reflect_batch(requests)

        # Verify item STILL EXISTS in specific vault
        async with metastore.session() as check_session:
            stmt = select(ReflectionQueue).where(col(ReflectionQueue.entity_id) == entity_id)
            item = (await check_session.exec(stmt)).first()
            assert item is not None, (
                'Queue item should still exist because it was processed with wrong vault_id'
            )

        # 4. Simulate Fixed CLI: Reflect with correct vault_id
        fixed_requests = [
            ReflectionRequest(entity_id=q.entity_id, vault_id=q.vault_id) for q in queue_items
        ]
        await api.reflect_batch(fixed_requests)

        # Verify item is GONE
        async with metastore.session() as check_session:
            stmt = select(ReflectionQueue).where(col(ReflectionQueue.entity_id) == entity_id)
            item = (await check_session.exec(stmt)).first()
            assert item is None, 'Queue item should have been cleared with correct vault_id'
