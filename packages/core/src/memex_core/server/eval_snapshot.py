"""Eval-only snapshot-import route.

This route is registered iff ``config.server.eval_mode=True`` at server
startup. Production servers never see it. The route is the entry point
that V12 callers (eval CLI, suite runner) use to skip LLM extraction by
restoring a pre-extracted snapshot into a fresh ephemeral vault.

Security: the route validates the snapshot path against an allowlist root
and uses ``O_NOFOLLOW`` on every file read (see ``path_validation.py``).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from memex_core.memory.sql_models import EMBEDDING_DIMENSION
from memex_core.services.snapshot.exporter import SnapshotExporter, SnapshotExportError
from memex_core.services.snapshot.manifest import EmbeddingModelIdentity
from memex_core.services.snapshot.path_validation import (
    SnapshotPathError,
    get_allowlist_root,
    validate_path_under_root,
)
from memex_core.services.snapshot.restore import (
    SnapshotImporter,
    SnapshotImportError,
    SnapshotImportRefused,
)

logger = logging.getLogger('memex.core.server.eval_snapshot')

router = APIRouter(prefix='/api/v1/_eval', tags=['eval'])


class SnapshotImportRequest(BaseModel):
    snapshot_path: str = Field(
        description=(
            'Server-local absolute path to the snapshot directory. Must '
            'reside under the allowlist root (default '
            '`~/.memex-eval/snapshots/`, override via '
            '`MEMEX_EVAL_SNAPSHOT_ROOT`).'
        ),
    )
    target_vault_name: str = Field(
        description=(
            'Name for the new ephemeral vault. The route always creates a '
            'fresh vault — there is no merge mode.'
        ),
    )


class SnapshotImportResponse(BaseModel):
    target_vault_id: UUID
    import_id: UUID


@router.post(
    '/snapshot-import',
    response_model=SnapshotImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def snapshot_import(request: Request, body: SnapshotImportRequest) -> SnapshotImportResponse:
    api = request.app.state.api
    config = api.config
    if not config.server.eval_mode:
        # Defensive — the router shouldn't be mounted when eval_mode is off,
        # but if a future code path bypasses that, refuse here.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Snapshot-import is disabled (server.eval_mode is False).',
        )

    try:
        allowlist_root = get_allowlist_root()
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Allowlist root unavailable: {e}',
        ) from e

    metastore = api.metastore
    try:
        async with metastore.session() as session:
            importer = SnapshotImporter(
                session=session,
                filestore=api.filestore,
                embedding_backend=config.server.embedding_model,
                snapshot_dir=body.snapshot_path,
                allowlist_root=allowlist_root,
                target_vault_name=body.target_vault_name,
            )
            target_vault_id = await importer.import_snapshot()
            return SnapshotImportResponse(
                target_vault_id=target_vault_id,
                import_id=importer.import_id,
            )
    except SnapshotImportRefused as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except SnapshotPathError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except SnapshotImportError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.exception('Snapshot import failed unexpectedly')
        # Don't leak internal exception text — could include filesystem
        # paths, DSN fragments, etc. The traceback is in the server logs.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Internal error during import; check server logs.',
        ) from e


# ----------------------------------------------------------------------
# Snapshot export — symmetric eval-mode-only endpoint used by the suite
# runner's ``--from-snapshot auto`` cache-populate path.


class SnapshotExportRequest(BaseModel):
    vault_id_or_name: str = Field(
        description='Vault UUID or name to export.',
    )
    output_path: str = Field(
        description=(
            'Server-local absolute path for the snapshot directory. Must '
            'reside under the allowlist root. The directory is created if '
            'absent; the route refuses to overwrite an existing snapshot '
            '(presence of `manifest.json` triggers refusal).'
        ),
    )


class SnapshotExportResponse(BaseModel):
    snapshot_path: str
    snapshot_version: str
    table_counts: dict[str, int]


@router.post(
    '/snapshot-export',
    response_model=SnapshotExportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def snapshot_export(request: Request, body: SnapshotExportRequest) -> SnapshotExportResponse:
    api = request.app.state.api
    config = api.config
    if not config.server.eval_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Snapshot-export is disabled (server.eval_mode is False).',
        )

    try:
        allowlist_root = get_allowlist_root()
        output_path = validate_path_under_root(body.output_path, allowlist_root=allowlist_root)
    except SnapshotPathError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Allowlist root unavailable: {e}',
        ) from e

    # Construct embedding identity from the live server config so the
    # manifest reflects what the server actually used.
    from memex_core.memory.models.base import MODEL_REGISTRY

    embedding_cfg = config.server.embedding_model
    if hasattr(embedding_cfg, 'type') and embedding_cfg.type == 'litellm':
        identity = EmbeddingModelIdentity(
            name=str(getattr(embedding_cfg, 'model', 'litellm:unknown')),
            dim=EMBEDDING_DIMENSION,
            hash='',
        )
    else:
        spec = MODEL_REGISTRY['embedding']
        identity = EmbeddingModelIdentity(
            name=str(spec.repo_id),
            dim=EMBEDDING_DIMENSION,
            hash=str(spec.revision),
        )

    metastore = api.metastore
    try:
        # Resolve UUID or pass through name.
        try:
            vault_selector: object = UUID(body.vault_id_or_name)
        except ValueError:
            vault_selector = body.vault_id_or_name

        async with metastore.session() as session:
            exporter = SnapshotExporter(
                session=session,
                filestore=api.filestore,
                vault_id_or_name=vault_selector,  # type: ignore[arg-type]
                output_dir=output_path,
                embedding_model=identity,
            )
            manifest = await exporter.export()
        return SnapshotExportResponse(
            snapshot_path=str(output_path),
            snapshot_version=manifest.snapshot_version,
            table_counts=manifest.table_counts,
        )
    except SnapshotExportError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except SnapshotPathError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.exception('Snapshot export failed unexpectedly')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Internal error during export; check server logs.',
        ) from e
