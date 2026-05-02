"""F32 concurrent cold-path + restart-recovery integration tests (Tests 3, 3b)."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone
from uuid import uuid4

import pytest

pytest.importorskip('umap')

from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

from memex_common.types import FactTypes  # noqa: E402
from memex_core.config import MemexConfig  # noqa: E402
from memex_core.diagnostics import umap as umap_mod  # noqa: E402
from memex_core.diagnostics.umap import cache_path_for  # noqa: E402
from memex_core.memory.sql_models import MemoryUnit, Note, Vault  # noqa: E402
from memex_core.services.diagnostics import DiagnosticsService  # noqa: E402


async def _seed_minimal_vault(session: AsyncSession) -> Vault:
    vault = Vault(name=f'F32-conc-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(id=uuid4(), vault_id=vault.id, content_hash='h', original_text='seed')
    session.add(note)
    await session.commit()

    now = datetime.now(timezone.utc)
    units = [
        MemoryUnit(
            id=uuid4(),
            vault_id=vault.id,
            note_id=note.id,
            text=f'unit {i}',
            fact_type=FactTypes.WORLD,
            status='active',
            event_date=now,
            embedding=[0.1 * (i + 1)] * 384,
        )
        for i in range(4)
    ]
    session.add_all(units)
    await session.commit()
    return vault


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_cold_requests_share_task(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
    monkeypatch,
):
    """Two concurrent cold GETs yield the same task_id and exactly ONE compute."""
    vault = await _seed_minimal_vault(session)

    counter = {'count': 0}
    real_compute = umap_mod.compute_manifold

    async def counting_compute(*args, **kwargs):
        counter['count'] += 1
        return await real_compute(*args, **kwargs)

    monkeypatch.setattr(
        'memex_core.services.diagnostics.compute_manifold',
        functools.partial(counting_compute),
    )

    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        results = await asyncio.gather(
            service.get_or_compute_manifold(vault.id),
            service.get_or_compute_manifold(vault.id),
        )
        statuses = [r[0] for r in results]
        task_ids = [r[1]['task_id'] for r in results]
        assert statuses == ['computing', 'computing']
        assert task_ids[0] == task_ids[1]

        # Wait for the in-flight task to settle.
        pending = list(service._pending.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        assert counter['count'] == 1
    finally:
        await service.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_loses_registry_fresh_task_id(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
):
    """A new DiagnosticsService instance has an empty registry; cold call fires fresh task."""
    vault = await _seed_minimal_vault(session)

    service_a = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        status_a, payload_a = await service_a.get_or_compute_manifold(vault.id)
        assert status_a == 'computing'
        task_a = payload_a['task_id']

        # Wait for service A's compute to finish + cache file to land.
        deadline = asyncio.get_event_loop().time() + 120.0
        while asyncio.get_event_loop().time() < deadline:
            if await filestore.exists(cache_path_for(vault.id)):
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError('cache did not warm in time')
    finally:
        await service_a.shutdown()

    # New service instance with the same metastore/filestore → registry is empty
    # but the cache file persists, so a fresh GET hits warm_cache_hit and returns ready.
    service_b = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        # Querying status with the OLD task_id on the new instance should fall
        # back to the cache (registry is empty) and return 'ready'.
        status_b, payload_b = await service_b.get_manifold_status(vault.id, task_a)
        assert status_b == 'ready'
        assert payload_b is not None

        # And get_or_compute_manifold on a warm cache returns ready immediately.
        status_c, payload_c = await service_b.get_or_compute_manifold(vault.id)
        assert status_c == 'ready'
        assert payload_c is not None
    finally:
        await service_b.shutdown()
