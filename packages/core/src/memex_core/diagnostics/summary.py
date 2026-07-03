from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import case, func, select

from memex_core.diagnostics.heatmap import compute_heatmap
from memex_core.diagnostics.lint_dashboard import pending_by_type as _lint_pending_by_type
from memex_core.diagnostics.umap import (
    load_cached_manifold,
)
from memex_core.memory.sql_models import ContentStatus, MemoryUnit

if TYPE_CHECKING:
    from memex_core.storage.filestore import FileStore
    from memex_core.storage.metastore import MetaStore

logger = logging.getLogger('memex.core.diagnostics.summary')


async def compute_diagnostics_summary(
    metastore: 'MetaStore',
    filestore: 'FileStore',
    vault_id: UUID,
    *,
    pending_compute: bool = False,
) -> dict[str, Any]:
    unit_counts = await _unit_counts_by_state(metastore, vault_id)
    avg_mw = await _avg_mw_score(metastore, vault_id)
    manifold_status, cluster_count = await _manifold_status(filestore, vault_id, pending_compute)
    heatmap = await compute_heatmap(metastore, vault_id, top_n=5)
    lint_pending_by_type = await _lint_pending_by_type(metastore, vault_id)

    return {
        'vault_id': str(vault_id),
        'as_of': datetime.now(timezone.utc).isoformat(),
        'manifold_status': manifold_status,
        'unit_counts': unit_counts,
        'lint_pending_by_type': lint_pending_by_type,
        'cluster_count': cluster_count,
        'avg_mw_score': avg_mw,
        'top_5_retrieved_entities': heatmap['entities'],
    }


async def _unit_counts_by_state(metastore: 'MetaStore', vault_id: UUID) -> dict[str, int]:
    active_expr = case(
        (
            (MemoryUnit.status == ContentStatus.ACTIVE) & (MemoryUnit.is_deprioritized.is_(False)),
            1,
        ),
        else_=0,
    )
    deprioritized_expr = case(
        (
            (MemoryUnit.status == ContentStatus.ACTIVE) & (MemoryUnit.is_deprioritized.is_(True)),
            1,
        ),
        else_=0,
    )
    stale_expr = case(
        ((MemoryUnit.status == ContentStatus.STALE), 1),
        else_=0,
    )
    stmt = select(
        func.coalesce(func.sum(active_expr), 0),
        func.coalesce(func.sum(deprioritized_expr), 0),
        func.coalesce(func.sum(stale_expr), 0),
    ).where(MemoryUnit.vault_id == vault_id)
    async with metastore.session() as sess:
        row = (await sess.exec(stmt)).one()
    return {
        'active': int(row[0] or 0),
        'deprioritized': int(row[1] or 0),
        'stale': int(row[2] or 0),
    }


async def _avg_mw_score(metastore: 'MetaStore', vault_id: UUID) -> float:
    stmt = select(
        func.avg(
            (MemoryUnit.success_co_count + 1.0)
            / (MemoryUnit.success_co_count + MemoryUnit.failure_co_count + 2.0)
        )
    ).where(
        MemoryUnit.vault_id == vault_id,
        MemoryUnit.status == ContentStatus.ACTIVE,
        MemoryUnit.is_deprioritized.is_(False),
    )
    async with metastore.session() as sess:
        row = (await sess.exec(stmt)).one()
    # .one() always returns a Row tuple; row[0] is the avg() scalar (None for empty).
    val = row[0]
    return float(val) if val is not None else 0.5


async def _manifold_status(
    filestore: 'FileStore',
    vault_id: UUID,
    pending_compute: bool,
) -> tuple[str, int | None]:
    if pending_compute:
        return 'pending', None
    cached = await load_cached_manifold(filestore, vault_id)
    if cached is None:
        return 'absent', None
    return 'ready', cached.get('cluster_count')
