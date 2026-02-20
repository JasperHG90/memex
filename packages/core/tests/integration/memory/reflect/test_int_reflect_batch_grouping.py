import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.sql_models import Entity, Vault
from memex_core.config import MemexConfig


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflect_batch_grouping(session: AsyncSession, memex_config: MemexConfig):
    """
    Test that reflect_batch correctly groups requests by vault and processes them.
    We mock _reflect_entity_internal to avoid LLM calls and just verify it's called with the right args.
    """
    # 1. Setup Vaults
    v1 = Vault(name='V1')
    v2 = Vault(name='V2')
    session.add_all([v1, v2])
    await session.commit()
    await session.refresh(v1)
    await session.refresh(v2)

    # 2. Setup Entities
    e1 = Entity(canonical_name='E1')
    e2 = Entity(canonical_name='E2')
    e3 = Entity(canonical_name='E3')
    session.add_all([e1, e2, e3])
    await session.commit()
    await session.refresh(e1)
    await session.refresh(e2)
    await session.refresh(e3)

    from unittest.mock import MagicMock

    mock_embedder = MagicMock()

    engine = ReflectionEngine(session=session, config=memex_config, embedder=mock_embedder)

    # 3. Create requests across different vaults
    # E1 in V1, E2 in V1, E3 in V2
    requests = [
        ReflectionRequest(entity_id=e1.id, vault_id=v1.id),
        ReflectionRequest(entity_id=e2.id, vault_id=v1.id),
        ReflectionRequest(entity_id=e3.id, vault_id=v2.id),
    ]

    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    with patch.object(engine, '_reflect_entity_internal', new_callable=AsyncMock) as mock_reflect:
        # Mock return values (MentalModels)
        from memex_core.memory.sql_models import MentalModel

        m1 = MentalModel(id=uuid4(), vault_id=v1.id, entity_id=e1.id, name='M1', observations=[])
        m2 = MentalModel(id=uuid4(), vault_id=v1.id, entity_id=e2.id, name='M2', observations=[])
        m3 = MentalModel(id=uuid4(), vault_id=v2.id, entity_id=e3.id, name='M3', observations=[])

        # Set mock to return some models
        mock_reflect.side_effect = [m1, m2, m3]

        # 4. Run reflect_batch
        requests = [
            ReflectionRequest(entity_id=e1.id, vault_id=v1.id),
            ReflectionRequest(entity_id=e2.id, vault_id=v1.id),
            ReflectionRequest(entity_id=e3.id, vault_id=v2.id),
        ]
        results = await engine.reflect_batch(requests)

        # 5. Verify results
        assert len(results) == 3
        assert mock_reflect.call_count == 3

        # Verify vault_id propagation
        calls = mock_reflect.call_args_list

    found_v1 = 0
    found_v2 = 0
    for call in calls:
        kwargs = call.kwargs
        if kwargs['vault_id'] == v1.id:
            found_v1 += 1
        elif kwargs['vault_id'] == v2.id:
            found_v2 += 1

    assert found_v1 == 2
    assert found_v2 == 1
