from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import func, select

from memex_core.memory.sql_models import MemoryUnit
from memex_core.metrics import (
    DIAGNOSTICS_CACHE_HITS_TOTAL,
    DIAGNOSTICS_CACHE_MISSES_TOTAL,
    DIAGNOSTICS_MANIFOLD_COMPUTE_SECONDS,
)

if TYPE_CHECKING:
    from memex_core.storage.filestore import FileStore
    from memex_core.storage.metastore import MetaStore

logger = logging.getLogger('memex.core.diagnostics.umap')
tracer = trace.get_tracer('memex.diagnostics')

DEFAULT_UMAP_PARAMS: dict[str, Any] = {
    'n_neighbors': 15,
    'min_dist': 0.1,
    'metric': 'cosine',
    'n_components': 2,
    'random_state': 42,
}


class UMAPNotInstalledError(RuntimeError):
    """Raised when umap-learn is requested but the optional extra is missing."""


def cache_key(
    vault_id: UUID,
    unit_count: int,
    last_updated_at: datetime | None,
    params: dict[str, Any] | None = None,
) -> str:
    p = dict(DEFAULT_UMAP_PARAMS)
    if params:
        p.update(params)
    payload = json.dumps(
        {
            'vault_id': str(vault_id),
            'unit_count': int(unit_count),
            'last_updated_at': last_updated_at.isoformat() if last_updated_at else None,
            'params': p,
        },
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def cache_path_for(vault_id: UUID) -> str:
    return f'vaults/{vault_id}/diagnostics/manifold.json'


async def _vault_unit_signature(
    metastore: 'MetaStore',
    vault_id: UUID,
) -> tuple[int, datetime | None]:
    async with metastore.session() as sess:
        stmt = select(
            func.count(MemoryUnit.id),
            func.max(MemoryUnit.updated_at),
        ).where(MemoryUnit.vault_id == vault_id)
        row = (await sess.exec(stmt)).one()
    count = int(row[0] or 0)
    last = row[1]
    return count, last


async def load_cached_manifold(
    filestore: 'FileStore',
    vault_id: UUID,
) -> dict[str, Any] | None:
    key = cache_path_for(vault_id)
    if not await filestore.exists(key):
        return None
    raw = await filestore.load(key)
    return json.loads(raw.decode('utf-8'))


async def compute_manifold(
    metastore: 'MetaStore',
    filestore: 'FileStore',
    vault_id: UUID,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import umap  # noqa: F401  — lazy import per [diagnostics] extra
    except ImportError as e:
        raise UMAPNotInstalledError(
            'umap-learn is not installed; install memex[diagnostics] to enable manifold endpoints.'
        ) from e

    p = dict(DEFAULT_UMAP_PARAMS)
    if params:
        p.update(params)

    unit_count, last_updated_at = await _vault_unit_signature(metastore, vault_id)
    key_hash = cache_key(vault_id, unit_count, last_updated_at, p)

    span_attrs = {
        'vault_id': str(vault_id),
        'n_units': unit_count,
        'cache_key': key_hash,
    }
    with tracer.start_as_current_span('memex.diagnostics.compute_manifold', attributes=span_attrs):
        with DIAGNOSTICS_MANIFOLD_COMPUTE_SECONDS.labels(vault_id=str(vault_id)).time():
            DIAGNOSTICS_CACHE_MISSES_TOTAL.labels(vault_id=str(vault_id)).inc()

            embeddings, unit_ids = await _load_embeddings(metastore, vault_id)
            if not embeddings:
                payload = {
                    'vault_id': str(vault_id),
                    'cache_key': key_hash,
                    'computed_at': datetime.now(timezone.utc).isoformat(),
                    'n_units': 0,
                    'umap_params': p,
                    'points': [],
                    'cluster_count': None,
                }
            else:
                import numpy as np
                from umap import UMAP

                arr = np.asarray(embeddings, dtype=float)
                reducer = UMAP(**p)
                projected = reducer.fit_transform(arr)
                points = [
                    {'unit_id': str(uid), 'x': float(projected[i, 0]), 'y': float(projected[i, 1])}
                    for i, uid in enumerate(unit_ids)
                ]
                payload = {
                    'vault_id': str(vault_id),
                    'cache_key': key_hash,
                    'computed_at': datetime.now(timezone.utc).isoformat(),
                    'n_units': len(points),
                    'umap_params': p,
                    'points': points,
                    'cluster_count': None,
                }

            data = json.dumps(payload).encode('utf-8')
            await filestore.save(cache_path_for(vault_id), data)
            return payload


async def _load_embeddings(
    metastore: 'MetaStore',
    vault_id: UUID,
) -> tuple[list[list[float]], list[UUID]]:
    async with metastore.session() as sess:
        stmt = (
            select(MemoryUnit.id, MemoryUnit.embedding)
            .where(MemoryUnit.vault_id == vault_id)
            .where(MemoryUnit.embedding.is_not(None))
        )
        rows = (await sess.exec(stmt)).all()
    embeddings: list[list[float]] = []
    unit_ids: list[UUID] = []
    for unit_id, emb in rows:
        if emb is None:
            continue
        embeddings.append(list(emb))
        unit_ids.append(unit_id)
    return embeddings, unit_ids


async def warm_cache_hit(
    filestore: 'FileStore',
    metastore: 'MetaStore',
    vault_id: UUID,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    cached = await load_cached_manifold(filestore, vault_id)
    if cached is None:
        return None
    unit_count, last_updated_at = await _vault_unit_signature(metastore, vault_id)
    expected = cache_key(vault_id, unit_count, last_updated_at, params)
    if cached.get('cache_key') != expected:
        return None
    DIAGNOSTICS_CACHE_HITS_TOTAL.labels(vault_id=str(vault_id)).inc()
    return cached
