"""Build the runtime resources `SnapshotImporter` / `SnapshotExporter` need.

The eval runner talks to a Memex server via HTTP for normal operations
(create vaults, ingest notes, search). For snapshot import/export it
goes around the HTTP layer entirely and uses the same DB + FileStore the
server is configured against. This module assembles those handles from
``MemexConfig.load()`` and yields them as an async context.

Why direct DB access: the snapshot importer writes many tables in a
single Postgres transaction (FK-safe), then runs REINDEX (which cannot
run inside a transaction), then writes assets through the FileStore.
Replicating that through public HTTP endpoints would either trigger
extraction (defeating the point) or require an extension API surface
neither side wants. Eval owns its snapshot lifecycle; it uses the
ordinary memex_core storage classes to do the work.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, NamedTuple

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import (
    LitellmEmbeddingBackend,
    MemexConfig,
    parse_memex_config,
)
from memex_core.memory.sql_models import EMBEDDING_DIMENSION
from memex_core.services.snapshot.manifest import EmbeddingModelIdentity
from memex_core.storage.filestore import BaseAsyncFileStore, get_filestore
from memex_core.storage.metastore import get_metastore

from memex_eval.snapshot.import_state import ensure_eval_import_state_table


class SnapshotRuntime(NamedTuple):
    config: MemexConfig
    session: AsyncSession
    filestore: BaseAsyncFileStore


@asynccontextmanager
async def snapshot_runtime() -> AsyncIterator[SnapshotRuntime]:
    """Yield (config, session, filestore) bound to the server's stores.

    Reads the standard ``MEMEX_*`` env vars / YAML config — the same
    settings the server uses — so eval and server agree on storage
    targets without extra configuration. Ensures the
    ``eval_import_state`` table exists on the DB (idempotent DDL) before
    yielding so callers don't have to.
    """
    config = parse_memex_config()
    metastore = get_metastore(config.server.meta_store)
    await metastore.connect(create_schema=False)
    try:
        filestore = get_filestore(config.server.file_store)

        async with metastore.engine.begin() as conn:
            await ensure_eval_import_state_table(conn)

        session_factory = metastore.session_maker()
        async with session_factory() as session:
            yield SnapshotRuntime(config=config, session=session, filestore=filestore)
    finally:
        await metastore.close()


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
