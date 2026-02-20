import pytest
from uuid import uuid4
from sqlmodel import select
from memex_core.memory.sql_models import Vault, Entity, ReflectionQueue, ReflectionStatus


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflection_queue_vault_isolation(session):
    """
    Verify that the ReflectionQueue can handle the same entity being queued
    in different vaults simultaneously (fixing the previous PK constraint issue).
    """
    # 1. Setup
    vault_a = Vault(id=uuid4(), name='Vault A')
    vault_b = Vault(id=uuid4(), name='Vault B')
    session.add(vault_a)
    session.add(vault_b)

    entity = Entity(canonical_name='Test Entity')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # 2. Add to Queue for Vault A
    item_a = ReflectionQueue(
        entity_id=entity.id,
        vault_id=vault_a.id,
        status=ReflectionStatus.PENDING,
        priority_score=10.0,
    )
    session.add(item_a)
    await session.commit()

    # 3. Add to Queue for Vault B (Should succeed now, previously would fail PK)
    item_b = ReflectionQueue(
        entity_id=entity.id,
        vault_id=vault_b.id,
        status=ReflectionStatus.PENDING,
        priority_score=5.0,
    )
    session.add(item_b)
    await session.commit()

    # 4. Verify both exist
    stmt = select(ReflectionQueue).where(ReflectionQueue.entity_id == entity.id)
    results = await session.exec(stmt)
    queue_items = results.all()

    assert len(queue_items) == 2
    assert any(q.vault_id == vault_a.id for q in queue_items)
    assert any(q.vault_id == vault_b.id for q in queue_items)

    # Verify IDs are different (Surrogate Key check)
    assert item_a.id != item_b.id
