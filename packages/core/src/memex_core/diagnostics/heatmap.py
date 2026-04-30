from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import desc, func, select

from memex_core.memory.sql_models import Entity, UnitEntity

if TYPE_CHECKING:
    from memex_core.storage.metastore import MetaStore

logger = logging.getLogger('memex.core.diagnostics.heatmap')


async def compute_heatmap(
    metastore: 'MetaStore',
    vault_id: UUID,
    top_n: int = 50,
) -> dict[str, Any]:
    volume = (UnitEntity.success_co_count + UnitEntity.failure_co_count).label('volume')
    avg_mw = func.avg(
        (UnitEntity.success_co_count + 1.0)
        / (UnitEntity.success_co_count + UnitEntity.failure_co_count + 2.0)
    ).label('avg_mw')

    async with metastore.session() as sess:
        stmt = (
            select(
                Entity.id,
                Entity.canonical_name,
                func.sum(volume).label('volume'),
                avg_mw,
            )
            .join(UnitEntity, UnitEntity.entity_id == Entity.id)
            .where(UnitEntity.vault_id == vault_id)
            .group_by(Entity.id, Entity.canonical_name)
            .order_by(desc('volume'), desc('avg_mw'))
            .limit(top_n)
        )
        rows = (await sess.exec(stmt)).all()

    entities = [
        {
            'entity_id': str(r[0]),
            'name': r[1],
            'volume': int(r[2] or 0),
            'avg_mw_score': float(r[3] or 0.5),
        }
        for r in rows
    ]
    return {
        'vault_id': str(vault_id),
        'as_of': datetime.now(timezone.utc).isoformat(),
        'top_n': top_n,
        'entities': entities,
    }
