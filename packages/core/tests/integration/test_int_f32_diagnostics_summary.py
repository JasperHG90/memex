"""F32 diagnostics summary — integration tests (Test 1, 1b)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.config import MemexConfig
from memex_core.memory.sql_models import MemoryUnit, Note, Vault
from memex_core.services.diagnostics import DiagnosticsService


async def _seed_vault_with_units(
    session: AsyncSession, *, n_active: int, n_stale: int, n_deprioritized: int
) -> Vault:
    vault = Vault(name=f'F32-test-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash='hash',
        original_text='seed note',
    )
    session.add(note)
    await session.commit()

    now = datetime.now(timezone.utc)
    units: list[MemoryUnit] = []
    for _ in range(n_active):
        units.append(
            MemoryUnit(
                id=uuid4(),
                vault_id=vault.id,
                note_id=note.id,
                text='active unit',
                fact_type=FactTypes.WORLD,
                status='active',
                is_deprioritized=False,
                event_date=now,
                embedding=[0.1] * 384,
                success_co_count=2,
                failure_co_count=1,
            )
        )
    for _ in range(n_stale):
        units.append(
            MemoryUnit(
                id=uuid4(),
                vault_id=vault.id,
                note_id=note.id,
                text='stale unit',
                fact_type=FactTypes.WORLD,
                status='stale',
                is_deprioritized=False,
                event_date=now,
                embedding=[0.1] * 384,
                success_co_count=0,
                failure_co_count=0,
            )
        )
    for _ in range(n_deprioritized):
        units.append(
            MemoryUnit(
                id=uuid4(),
                vault_id=vault.id,
                note_id=note.id,
                text='deprioritized unit',
                fact_type=FactTypes.WORLD,
                status='active',
                is_deprioritized=True,
                event_date=now,
                embedding=[0.1] * 384,
                success_co_count=1,
                failure_co_count=3,
            )
        )
    session.add_all(units)
    await session.commit()
    return vault


@pytest.mark.integration
@pytest.mark.asyncio
async def test_returns_all_fields_with_cluster_count_null(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
):
    """Cold-cache summary returns all documented keys; cluster_count is null;
    manifold_status ∈ {'pending','absent'}; unit_counts has active/stale/deprioritized."""
    vault = await _seed_vault_with_units(session, n_active=3, n_stale=1, n_deprioritized=2)

    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    summary = await service.get_summary(vault.id)

    assert summary['vault_id'] == str(vault.id)
    assert 'as_of' in summary
    assert summary['manifold_status'] in {'pending', 'absent'}
    assert summary['cluster_count'] is None

    counts = summary['unit_counts']
    assert set(counts.keys()) == {'active', 'stale', 'deprioritized'}
    assert counts['active'] == 3
    assert counts['stale'] == 1
    assert counts['deprioritized'] == 2

    assert 'lint_pending_by_type' in summary
    assert summary['lint_pending_by_type'] == {}
    assert 'avg_mw_score' in summary
    assert isinstance(summary['avg_mw_score'], float)
    assert 'top_5_retrieved_entities' in summary
    assert isinstance(summary['top_5_retrieved_entities'], list)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize('with_f6_seed', [False, True])
async def test_lint_pending_by_type_pre_post_f6(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config: MemexConfig,
    with_f6_seed: bool,
):
    """Pre-F6: lint_pending_by_type is {} (key always present).
    Post-F6: skipped until MaintenanceProposal lands."""
    if with_f6_seed:
        try:
            from memex_core.memory.sql_models import MaintenanceProposal  # noqa: F401
        except ImportError:
            pytest.skip('MaintenanceProposal table absent; F6 not yet shipped')

    vault = await _seed_vault_with_units(session, n_active=1, n_stale=0, n_deprioritized=0)

    service = DiagnosticsService(metastore=metastore, filestore=filestore, config=memex_config)
    summary = await service.get_summary(vault.id)

    assert 'lint_pending_by_type' in summary
    if not with_f6_seed:
        assert summary['lint_pending_by_type'] == {}
