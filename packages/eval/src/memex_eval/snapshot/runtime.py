"""Build the runtime resources `SnapshotImporter` / `SnapshotExporter` need.

The eval runner talks to a Memex server via HTTP for normal operations
(create vaults, ingest notes, search). For snapshot import/export it
goes around the HTTP layer entirely and uses the same DB + FileStore the
server is configured against. This module assembles those handles from
the eval process's environment (``parse_memex_config()``) and yields
them as an async context.

Why direct DB access: the snapshot importer writes many tables in a
single Postgres transaction (FK-safe), then runs REINDEX (which cannot
run inside a transaction), then writes assets through the FileStore.
Replicating that through public HTTP endpoints would either trigger
extraction (defeating the point) or require an extension API surface
neither side wants. Eval owns its snapshot lifecycle; it uses the
ordinary memex_core storage classes to do the work.

Pre-flight: the runner must call ``check_runtime_matches_server`` against
the live server's ``/system/config`` BEFORE entering ``snapshot_runtime``.
The check refuses when the eval-side env points at a different DB or
embedding model than the running server (in which case the in-process
writes would land somewhere other than what the server is reading).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, NamedTuple

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import (
    LitellmEmbeddingBackend,
    MemexConfig,
    parse_memex_config,
)
from memex_core.memory.sql_models import EMBEDDING_DIMENSION
from memex_core.migration import get_expected_head
from memex_core.services.snapshot.manifest import EmbeddingModelIdentity
from memex_core.storage.filestore import BaseAsyncFileStore, get_filestore
from memex_core.storage.metastore import get_metastore

from memex_eval.snapshot.import_state import ensure_eval_import_state_table


class SnapshotRuntimeMismatch(RuntimeError):
    """Raised when the eval-side env doesn't match the running server.

    The in-process snapshot importer/exporter writes directly to the
    DB pointed to by ``parse_memex_config()``; if the server is reading
    from a different DB, those writes are invisible to scenarios. Same
    for the embedding model (regenerated vectors must be comparable).
    """


class SnapshotRuntime(NamedTuple):
    config: MemexConfig
    session: AsyncSession
    filestore: BaseAsyncFileStore


def check_runtime_matches_server(server_url: str, server_config_snapshot: dict[str, Any]) -> None:
    """Refuse if the eval-side env doesn't match the running server.

    Two checks:

    1. ``--server`` must point at a loopback host. The eval-side env
       resolves to the LOCAL DB / FileStore via ``parse_memex_config()``
       — that DB is only the same as a remote server's DB if the user
       has explicitly arranged for them to match, and we can't validate
       that reliably across the wire. Easiest safe contract: refuse
       non-localhost servers.
    2. The local config's embedding-model identity must match the
       server-reported one. Mismatched embedding models silently corrupt
       retrieval — the imported vectors are incomparable.

    Raises ``SnapshotRuntimeMismatch`` with an actionable message on
    either check.
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(server_url)
    if not parsed.scheme or not parsed.hostname:
        raise SnapshotRuntimeMismatch(
            f'--from-snapshot requires --server to include a scheme and '
            f'host (got {server_url!r}). Use e.g. '
            f'http://localhost:8000/api/v1/.'
        )
    host = parsed.hostname.lower()
    is_loopback = host == 'localhost'
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise SnapshotRuntimeMismatch(
            f'--from-snapshot requires --server to point at a loopback '
            f'host (got {host!r}). The eval runner writes snapshots '
            f'directly to the DB resolved from the LOCAL environment via '
            f'parse_memex_config(); running against a remote server '
            f'risks writing to a different DB than the server reads.'
        )

    local_config = parse_memex_config()
    local_emb = _embedding_identity_string(local_config)
    remote_emb = _embedding_identity_from_snapshot(server_config_snapshot)
    if remote_emb and local_emb != remote_emb:
        raise SnapshotRuntimeMismatch(
            f'Embedding model divergence: server reports {remote_emb!r} '
            f'but local config resolves to {local_emb!r}. Snapshot '
            f'embeddings would be incomparable. Align '
            f'MEMEX_SERVER__EMBEDDING_MODEL__* on the eval-side env '
            f'with the server.'
        )


def _embedding_identity_string(config: MemexConfig) -> str:
    backend = config.server.embedding_model
    if isinstance(backend, LitellmEmbeddingBackend):
        return f'litellm:{getattr(backend, "model", "unknown")}'
    from memex_core.memory.models.base import MODEL_REGISTRY

    spec = MODEL_REGISTRY['embedding']
    return f'onnx:{spec.repo_id}@{spec.revision}'


def _embedding_identity_from_snapshot(snapshot: dict[str, Any]) -> str | None:
    """Best-effort identity extraction from `/system/config` payload.

    Returns None when the snapshot's shape doesn't expose enough to
    compare — callers treat that as "skip the check".
    """
    emb = snapshot.get('server', {}).get('embedding_model') or {}
    backend_type = emb.get('type')
    if backend_type == 'litellm':
        model = emb.get('model') or 'unknown'
        return f'litellm:{model}'
    if backend_type == 'onnx':
        from memex_core.memory.models.base import MODEL_REGISTRY

        spec = MODEL_REGISTRY['embedding']
        return f'onnx:{spec.repo_id}@{spec.revision}'
    return None


@asynccontextmanager
async def snapshot_runtime() -> AsyncIterator[SnapshotRuntime]:
    """Yield (config, session, filestore) bound to the server's stores.

    Reads the standard ``MEMEX_*`` env vars / YAML config — the same
    settings the server uses — so eval and server agree on storage
    targets without extra configuration. Ensures the
    ``eval_import_state`` table exists on the DB (idempotent DDL) before
    yielding so callers don't have to. Validates the live DB's alembic
    head against the script-dir head before yielding so a populate
    against a stale schema fails fast.
    """
    config = parse_memex_config()
    metastore = get_metastore(config.server.meta_store)
    await metastore.connect(create_schema=False)
    try:
        filestore = get_filestore(config.server.file_store)

        async with metastore.engine.begin() as conn:
            await _verify_alembic_head(conn)
            await ensure_eval_import_state_table(conn)

        session_factory = metastore.session_maker()
        async with session_factory() as session:
            yield SnapshotRuntime(config=config, session=session, filestore=filestore)
    finally:
        await metastore.close()


async def _verify_alembic_head(conn: Any) -> None:
    expected = get_expected_head()
    result = await conn.execute(text('SELECT version_num FROM alembic_version'))
    row = result.first()
    db_head = row[0] if row is not None else None
    if db_head is None:
        raise SnapshotRuntimeMismatch(
            'No alembic_version row on the importing DB. Run `just db-upgrade` first.'
        )
    if db_head != expected:
        raise SnapshotRuntimeMismatch(
            f'Alembic head mismatch: db={db_head} expected={expected}. '
            f'Run `just db-upgrade` to migrate.'
        )


def build_embedding_identity(config: MemexConfig) -> EmbeddingModelIdentity:
    """Snapshot manifest's embedding-identity from the live config.

    Mirrors what the exporter needs to write the manifest's
    ``embedding_model`` block. The eval runner uses this when populating
    the cache via the in-process ``SnapshotExporter``.
    """
    backend = config.server.embedding_model
    if isinstance(backend, LitellmEmbeddingBackend):
        return EmbeddingModelIdentity(
            name=str(getattr(backend, 'model', 'litellm:unknown')),
            dim=EMBEDDING_DIMENSION,
            hash='',
        )
    # Local (ONNX) — pull name/revision from the model registry.
    from memex_core.memory.models.base import MODEL_REGISTRY

    spec = MODEL_REGISTRY['embedding']
    return EmbeddingModelIdentity(
        name=str(spec.repo_id),
        dim=EMBEDDING_DIMENSION,
        hash=str(spec.revision),
    )
