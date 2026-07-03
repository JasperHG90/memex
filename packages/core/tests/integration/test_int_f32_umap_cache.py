"""F32 UMAP cache — cold/warm/status integration tests (Test 2)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.config import MemexConfig
from memex_core.diagnostics.umap import cache_path_for
from memex_core.memory.sql_models import MemoryUnit, Note, Vault
from memex_core.services.diagnostics import DiagnosticsService


async def _seed_minimal_vault(session: AsyncSession) -> Vault:
    vault = Vault(name=f'F32-cache-{uuid4().hex[:8]}')
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
        for i in range(5)
    ]
    session.add_all(units)
    await session.commit()
    return vault


async def _wait_for_cache(filestore, vault_id, timeout: float = 5.0) -> None:
    path = cache_path_for(vault_id)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await filestore.exists(path):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f'Cache file did not appear: {path}')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cold_kicks_off_compute_returns_task_id(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
):
    """First request on cold cache returns ('computing', {task_id})."""
    vault = await _seed_minimal_vault(session)
    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        status, payload = await service.get_or_compute_manifold(vault.id)
        assert status == 'computing'
        assert 'task_id' in payload
        assert isinstance(payload['task_id'], str)
    finally:
        await service.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_warm_after_compute_returns_ready(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
):
    """After compute completes, second call returns ('ready', cached_payload)."""
    # Manifold compute needs umap-learn (the optional [diagnostics] extra); when
    # it isn't installed the background compute can't write the cache. Gate on it.
    pytest.importorskip('umap', reason='requires memex[diagnostics] (umap-learn)')
    vault = await _seed_minimal_vault(session)
    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        status1, _ = await service.get_or_compute_manifold(vault.id)
        assert status1 == 'computing'

        await _wait_for_cache(filestore, vault.id, timeout=120.0)

        status2, payload2 = await service.get_or_compute_manifold(vault.id)
        assert status2 == 'ready'
        assert payload2 is not None
        assert payload2['vault_id'] == str(vault.id)
        assert 'cache_key' in payload2
        assert 'points' in payload2
    finally:
        await service.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_returns_ready_after_compute(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
):
    """status endpoint backing returns 'ready' once compute is done."""
    pytest.importorskip('umap', reason='requires memex[diagnostics] (umap-learn)')
    vault = await _seed_minimal_vault(session)
    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    try:
        status1, payload1 = await service.get_or_compute_manifold(vault.id)
        assert status1 == 'computing'
        task_id = payload1['task_id']

        await _wait_for_cache(filestore, vault.id, timeout=120.0)
        await asyncio.sleep(0.1)

        # status() with the original task_id should resolve to ready (the
        # task ran to completion and registry callback cleared it; the cache
        # file exists, so warm_cache_hit returns the payload).
        status2, payload2 = await service.get_manifold_status(vault.id, task_id)
        assert status2 == 'ready'
        assert payload2 is not None
        assert payload2['vault_id'] == str(vault.id)
    finally:
        await service.shutdown()
