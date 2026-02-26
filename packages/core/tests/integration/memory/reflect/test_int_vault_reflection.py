import pytest
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit, Entity, Vault, Note
from memex_common.config import MemexConfig, GLOBAL_VAULT_ID


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflection_engine_vault_isolation(session: AsyncSession, memex_config: MemexConfig):
    """
    Test that reflection in one vault does not see memories from another vault,
    but DOES see global memories (Fall-through logic).
    """
    # 0. Setup Dummy Document
    doc = Note(id=uuid4(), original_text='Dummy', content_hash='abc')
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # 1. Setup Vaults
    vault_a = Vault(name='Vault A')
    vault_b = Vault(name='Vault B')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()
    await session.refresh(vault_a)
    await session.refresh(vault_b)

    # 2. Setup Entity (Global)
    entity = Entity(canonical_name='VaultTestEntity')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # 3. Setup Memories
    dummy_embedding = [0.1] * 384

    # Global memory
    mem_global = MemoryUnit(
        id=uuid4(),
        text='Global fact: VaultTestEntity likes apples.',
        event_date=datetime.now(timezone.utc),
        vault_id=GLOBAL_VAULT_ID,
        fact_type=FactTypes.WORLD,
        note_id=doc.id,
        embedding=dummy_embedding,
    )
    # Vault A memory
    mem_a = MemoryUnit(
        id=uuid4(),
        text='Vault A fact: VaultTestEntity lives in Paris.',
        event_date=datetime.now(timezone.utc),
        vault_id=vault_a.id,
        fact_type=FactTypes.WORLD,
        note_id=doc.id,
        embedding=dummy_embedding,
    )
    # Vault B memory
    mem_b = MemoryUnit(
        id=uuid4(),
        text='Vault B fact: VaultTestEntity lives in Berlin.',
        event_date=datetime.now(timezone.utc),
        vault_id=vault_b.id,
        fact_type=FactTypes.WORLD,
        note_id=doc.id,
        embedding=dummy_embedding,
    )
    session.add_all([mem_global, mem_a, mem_b])
    await session.commit()

    # Associate memories with entity (via UnitEntity link)
    from memex_core.memory.sql_models import UnitEntity

    session.add_all(
        [
            UnitEntity(entity_id=entity.id, unit_id=mem_global.id),
            UnitEntity(entity_id=entity.id, unit_id=mem_a.id),
            UnitEntity(entity_id=entity.id, unit_id=mem_b.id),
        ]
    )
    await session.commit()

    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    engine = ReflectionEngine(session=session, config=memex_config, embedder=mock_embedder)

    # 4. Reflect in Vault A
    memories_map = await engine._batch_fetch_recent_memories([entity.id], vault_id=vault_a.id)
    mems = memories_map[entity.id]

    texts = [m.text for m in mems]
    assert any('Global fact' in t for t in texts)
    assert any('Vault A fact' in t for t in texts)
    assert not any('Vault B fact' in t for t in texts), (
        'Vault A reflection should not see Vault B memories'
    )

    # 5. Reflect in Vault B
    memories_map_b = await engine._batch_fetch_recent_memories([entity.id], vault_id=vault_b.id)
    mems_b = memories_map_b[entity.id]
    texts_b = [m.text for m in mems_b]
    assert any('Global fact' in t for t in texts_b)
    assert any('Vault B fact' in t for t in texts_b)
    assert not any('Vault A fact' in t for t in texts_b), (
        'Vault B reflection should not see Vault A memories'
    )

    # 6. Reflect in Global
    memories_map_g = await engine._batch_fetch_recent_memories(
        [entity.id], vault_id=GLOBAL_VAULT_ID
    )
    mems_g = memories_map_g[entity.id]
    texts_g = [m.text for m in mems_g]
    assert any('Global fact' in t for t in texts_g)
    assert not any('Vault A fact' in t for t in texts_g)
    assert not any('Vault B fact' in t for t in texts_g), (
        'Global reflection should only see global memories'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflection_engine_mental_model_isolation(
    session: AsyncSession, memex_config: MemexConfig
):
    """
    Test that mental models are isolated by vault.
    """
    # 1. Setup Vaults
    vault_a = Vault(name='Vault A')
    vault_b = Vault(name='Vault B')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()
    await session.refresh(vault_a)
    await session.refresh(vault_b)

    # 2. Setup Entity
    entity = Entity(canonical_name='ModelTestEntity')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    engine = ReflectionEngine(session=session, config=memex_config, embedder=mock_embedder)

    # 3. Create model in Vault A
    model_a = await engine._get_or_create_mental_model(entity.id, vault_id=vault_a.id)
    assert model_a.vault_id == vault_a.id

    # 4. Create model in Vault B
    model_b = await engine._get_or_create_mental_model(entity.id, vault_id=vault_b.id)
    assert model_b.vault_id == vault_b.id
    assert model_a.id != model_b.id

    # 5. Check Global
    model_g = await engine._get_or_create_mental_model(entity.id, vault_id=GLOBAL_VAULT_ID)
    assert model_g.vault_id == GLOBAL_VAULT_ID
    assert model_g.id != model_a.id
    assert model_g.id != model_b.id
