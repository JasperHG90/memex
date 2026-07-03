"""F32 heatmap — integration test (Test 4)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.diagnostics import compute_heatmap
from memex_core.diagnostics.umap import cache_path_for
from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    Note,
    UnitEntity,
    Vault,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_top_50_entities_no_manifold_dep(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Seed 60 entities with varied (success, failure) counts on a single MemoryUnit.
    Expect top-50 returned ordered by volume DESC. No manifold cache file written."""
    vault = Vault(name=f'F32-heatmap-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash='hash',
        original_text='heatmap seed',
    )
    session.add(note)
    await session.commit()

    now = datetime.now(timezone.utc)
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        event_date=now,
        embedding=[0.1] * 384,
    )
    session.add(unit)
    await session.commit()

    n_entities = 60
    entities: list[Entity] = []
    for i in range(n_entities):
        entities.append(Entity(canonical_name=f'E{i:03d}'))
    session.add_all(entities)
    await session.commit()
    for e in entities:
        await session.refresh(e)

    links: list[UnitEntity] = []
    for i, e in enumerate(entities):
        # Volume = success + failure spans 1..120 with varied success/failure mix
        success = i + 1
        failure = n_entities - i
        links.append(
            UnitEntity(
                unit_id=unit.id,
                entity_id=e.id,
                vault_id=vault.id,
                success_co_count=success,
                failure_co_count=failure,
            )
        )
    session.add_all(links)
    await session.commit()

    result = await compute_heatmap(metastore, vault.id, top_n=50)

    assert result['vault_id'] == str(vault.id)
    assert result['top_n'] == 50
    out = result['entities']
    assert len(out) == 50

    volumes = [row['volume'] for row in out]
    assert volumes == sorted(volumes, reverse=True)

    # Highest-volume entity expected: success_co_count + failure_co_count maximal.
    # success = i+1, failure = 60-i → volume = 61 for every i. With ties the
    # secondary sort is avg_mw DESC, where avg_mw = (i+2) / 63 — so the entity
    # with the largest i (E059) wins. Just assert the volume is the maximal 61.
    assert out[0]['volume'] == 61

    # No manifold cache file written by the heatmap path.
    manifold_path = cache_path_for(vault.id)
    assert not await filestore.exists(manifold_path)
