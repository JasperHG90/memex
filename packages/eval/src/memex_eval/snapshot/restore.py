"""V12 — eval-only snapshot import orchestrator.

In-process importer that reverses ``SnapshotExporter`` (V3) into a fresh
ephemeral vault on the target DB. Called from the eval suite runner via
``memex_eval.snapshot.runtime.snapshot_runtime``, which yields the DB
session, FileStore, and embedding-backend handles built from
``parse_memex_config()``.

Five (six with cache-populate) phases — see ``BACKLOG.md`` plan Decision 10:

* Pre-phase: allocate ``import_id`` and ``target_vault_id``; record state
  ``staging`` in ``eval_import_state``.
* Phase A — FileStore staging: copy assets into ``_staging/<import_id>/``
  on the FileStore. Idempotent on retry.
* Phase B — DB transaction: insert all vault-scoped rows in FK-safe order
  with explicit columns (preserves ``created_at``, MW counters, UUIDs).
  Asset/file-store columns are written with their final paths so the DB
  never references the staging location. Embedding columns are NULL.
* Phase C — FileStore commit: rename staging -> final paths.
* Phase D — Embeddings + REINDEX: run the local embedding model over the
  text columns, fill the NULL embedding columns in batches, REINDEX HNSW
  indexes. No transaction — idempotent retry of NULL rows.
* Phase E — Mark complete: ``state='complete'`` in ``eval_import_state``.
  The route returns success only after this row is observed.

Out of scope: merge mode, per-vault rebinding, multi-snapshot composition,
sync-engine compatibility. Those concerns are why V12 is eval-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import insert as sa_insert
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import (
    GLOBAL_VAULT_ID,
    GLOBAL_VAULT_NAME,
    EmbeddingBackend,
    LitellmEmbeddingBackend,
    OnnxBackend,
)
from memex_core.memory.formatting import format_for_embedding
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.sql_models import (
    Chunk,
    Entity,
    EntityAlias,
    EntityCooccurrence,
    MaintenanceProposal,
    MemoryLink,
    MemoryUnit,
    MentalModel,
    Node,
    Note,
    NoteAppend,
    ProcedureOutcome,
    UnitEntity,
    Vault,
    VaultSummary,
)
from memex_eval.snapshot.import_models import (
    PINNED_SNAPSHOT_MAJOR,
    PINNED_SNAPSHOT_MINOR,
    ChunkImport,
    EntityAliasImport,
    EntityCooccurrenceImport,
    EntityImport,
    MaintenanceProposalImport,
    MemoryLinkImport,
    MemoryUnitImport,
    MentalModelImport,
    NodeImport,
    NoteAppendImport,
    NoteImport,
    ObservationV1,
    ProcedureOutcomeImport,
    SnapshotManifestImport,
    UnitEntityImport,
    VaultImport,
    VaultSummaryImport,
)
from memex_core.services.snapshot.enum_coerce import coerce_enum_value
from memex_eval.snapshot.import_state import VALID_STATES
from memex_core.services.snapshot.manifest import (
    OBSERVATION_SCHEMA_VERSION,
    SnapshotVersion,
)
from memex_core.storage.filestore import BaseAsyncFileStore

logger = logging.getLogger('memex_eval.snapshot.restore')

RESERVED_VAULT_NAMES = {GLOBAL_VAULT_NAME.lower(), 'global', 'default'}

EMBED_BATCH_SIZE = 64


class SnapshotImportError(Exception):
    """Raised when an import fails validation or execution."""


class SnapshotImportRefused(SnapshotImportError):
    """Raised when an import is refused by policy (version, vault, mode)."""


# ----------------------------------------------------------------------
# Manifest + version validation


def _resolve_snapshot_dir(snapshot_dir: str | Path) -> Path:
    """Resolve and sanity-check the snapshot directory.

    In-process eval: there is no untrusted caller — the user supplies the
    path via CLI. We only need to confirm it exists and is a directory.
    """
    candidate = Path(snapshot_dir).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise SnapshotImportError(f'Snapshot directory not found: {snapshot_dir}') from e
    if not resolved.is_dir():
        raise SnapshotImportError(f'Snapshot path is not a directory: {resolved}')
    return resolved


async def _read_manifest(snapshot_dir: Path) -> SnapshotManifestImport:
    raw = (snapshot_dir / 'manifest.json').read_text(encoding='utf-8')
    data = json.loads(raw)
    # SnapshotManifestImport allows ``extra='ignore'`` so a v1.2+ manifest
    # with new fields parses cleanly on a v1.1-pinned importer.
    return SnapshotManifestImport.model_validate(data)


def _check_version(manifest: SnapshotManifestImport) -> None:
    parsed = SnapshotVersion.parse(manifest.snapshot_version)
    if parsed.major != PINNED_SNAPSHOT_MAJOR:
        raise SnapshotImportRefused(
            f'Snapshot MAJOR={parsed.major} != pinned {PINNED_SNAPSHOT_MAJOR}. '
            'Rebuild the snapshot with the matching exporter or upgrade the importer.'
        )
    if parsed.minor < PINNED_SNAPSHOT_MINOR:
        raise SnapshotImportRefused(
            f'Snapshot MINOR={parsed.minor} < pinned {PINNED_SNAPSHOT_MINOR}. '
            'Older minors may be missing required fields; refusing.'
        )


async def _check_alembic_head(session: AsyncSession, manifest: SnapshotManifestImport) -> None:
    """Compare manifest's alembic head to the actual DB head.

    Uses the live row in ``alembic_version`` — NOT the script-dir head — so
    a DB that has fallen behind raises here instead of silently importing.
    """
    result = await session.execute(text('SELECT version_num FROM alembic_version'))
    db_head = result.scalar_one_or_none()
    if db_head is None:
        raise SnapshotImportRefused(
            'No alembic_version row on the importing DB. Run `just db-upgrade` first.'
        )
    if db_head != manifest.alembic_head:
        raise SnapshotImportRefused(
            f'Alembic head mismatch: snapshot={manifest.alembic_head} db={db_head}. '
            'Upgrade the DB or rebuild the snapshot at the matching head.'
        )


def _check_embedding_model(
    manifest: SnapshotManifestImport, server_backend: EmbeddingBackend
) -> None:
    """Refuse on any embedding-model identity divergence.

    Refuses LiteLLM at import time (Decision 5): re-embedding via a remote
    backend is non-determistic across runs and burns API budget.
    """
    if isinstance(server_backend, LitellmEmbeddingBackend):
        raise SnapshotImportRefused(
            'Remote (LiteLLM) embedding backend is disallowed for snapshot import. '
            'Configure a local ONNX embedding model (`server.embedding_model.type=onnx`).'
        )
    if not isinstance(server_backend, OnnxBackend):
        raise SnapshotImportRefused(
            f'Unsupported embedding backend type: {type(server_backend).__name__}.'
        )

    # ONNX path: cross-check name/dim/hash against the manifest. Backend may
    # not expose dim directly; we trust the export-side identity (recorded
    # via MODEL_REGISTRY['embedding'] in the exporter at write time) and
    # re-check after the actual model loads in Phase D.
    from memex_core.memory.models.base import MODEL_REGISTRY

    spec = MODEL_REGISTRY['embedding']
    expected_name = str(spec.repo_id)
    expected_hash = str(spec.revision)
    if manifest.embedding_model.name != expected_name:
        raise SnapshotImportRefused(
            f'Embedding-model name mismatch: snapshot={manifest.embedding_model.name!r} '
            f'server={expected_name!r}.'
        )
    if manifest.embedding_model.hash != expected_hash:
        raise SnapshotImportRefused(
            f'Embedding-model revision mismatch: snapshot={manifest.embedding_model.hash!r} '
            f'server={expected_hash!r}.'
        )


def _check_observation_schema(manifest: SnapshotManifestImport) -> None:
    if manifest.observation_schema_version != OBSERVATION_SCHEMA_VERSION:
        raise SnapshotImportRefused(
            f'observation_schema_version mismatch: '
            f'snapshot={manifest.observation_schema_version!r} '
            f'pinned={OBSERVATION_SCHEMA_VERSION!r}.'
        )


def _check_vault_not_global(vault: VaultImport) -> None:
    if vault.id == GLOBAL_VAULT_ID:
        raise SnapshotImportRefused('Refusing to import the global vault.')
    if vault.name.lower() in RESERVED_VAULT_NAMES:
        raise SnapshotImportRefused(
            f'Refusing to import a snapshot whose source vault name {vault.name!r} '
            f'is reserved (matches {sorted(RESERVED_VAULT_NAMES)}).'
        )


# ----------------------------------------------------------------------
# JSONL helpers


def _read_jsonl_lines(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    raw = path.read_text(encoding='utf-8')
    for idx, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SnapshotImportError(f'Invalid JSON in {path.name} line {idx + 1}: {e}') from e
        # Decision 14: NaN/Inf forbidden in any field. Walk the parsed dict
        # so nested JSONB blobs (unit_metadata, link_metadata, observation
        # evidence) are also checked, not just the top-level pydantic
        # fields explicitly hand-rolled below.
        _check_no_nan_floats(obj, where=f'{path.name}[{idx + 1}]')
        yield obj


_NAN_CHECK_MAX_DEPTH = 64


def _check_no_nan_floats(value: Any, *, where: str, depth: int = 0) -> None:
    """Reject NaN/Inf in numeric fields (Decision 14).

    Capped at ``_NAN_CHECK_MAX_DEPTH`` so a maliciously deep JSON object
    cannot exhaust Python's recursion limit before any structured error
    surfaces.
    """
    if depth > _NAN_CHECK_MAX_DEPTH:
        raise SnapshotImportError(
            f'JSON depth > {_NAN_CHECK_MAX_DEPTH} in {where}; refusing to recurse further.'
        )
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise SnapshotImportError(f'NaN/Infinity in {where}')
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_no_nan_floats(v, where=f'{where}.{k}', depth=depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_no_nan_floats(v, where=f'{where}[{i}]', depth=depth + 1)


# ----------------------------------------------------------------------
# Eval import state helpers


async def _lookup_existing_import(
    session: AsyncSession, source_snapshot_path: Path
) -> tuple[UUID, UUID, str] | None:
    """Look up an existing import row keyed by ``source_snapshot_path``.

    Returns ``(target_vault_id, import_id, state)`` if a row exists for
    this source path, else None. Caller decides whether to refuse
    (state='complete') or resume (any other state) — see Decision 10.
    """
    result = await session.execute(
        text(
            'SELECT target_vault_id, import_id, state FROM eval_import_state '
            'WHERE source_snapshot_path = :path'
        ),
        {'path': str(source_snapshot_path)},
    )
    row = result.first()
    if row is None:
        return None
    return (UUID(str(row[0])), UUID(str(row[1])), str(row[2]))


def _refuse_completed_import(target_vault_id: UUID, source: Path) -> None:
    raise SnapshotImportRefused(
        f'Cannot import: snapshot at {source} already imported into vault '
        f'{target_vault_id} (state=complete). Each DB holds at most one '
        f'live import per source snapshot. To re-import, delete the vault '
        f'first: `memex vault delete {target_vault_id}`.'
    )


async def _record_import_state(
    session: AsyncSession,
    target_vault_id: UUID,
    import_id: UUID,
    snapshot_path: Path,
    state: str,
) -> None:
    if state not in VALID_STATES:
        raise ValueError(f'Invalid import state: {state}')
    await session.execute(
        text(
            'INSERT INTO eval_import_state '
            '(target_vault_id, import_id, source_snapshot_path, state, updated_at) '
            'VALUES (:vid, :iid, :path, :state, NOW()) '
            'ON CONFLICT (target_vault_id) DO UPDATE SET '
            'import_id = EXCLUDED.import_id, '
            'source_snapshot_path = EXCLUDED.source_snapshot_path, '
            'state = EXCLUDED.state, '
            'updated_at = NOW()'
        ),
        {
            'vid': str(target_vault_id),
            'iid': str(import_id),
            'path': str(snapshot_path),
            'state': state,
        },
    )


# ----------------------------------------------------------------------
# Main entry point


class SnapshotImporter:
    """Orchestrates the 5-phase eval-only snapshot import.

    One instance per import. ``import_snapshot()`` is the single entry
    point; it owns the lifecycle of ``import_id`` and ``target_vault_id``.
    """

    def __init__(
        self,
        session: AsyncSession,
        filestore: BaseAsyncFileStore | None,
        embedding_backend: EmbeddingBackend,
        snapshot_dir: Path,
        target_vault_name: str,
    ) -> None:
        self._session = session
        self._filestore = filestore
        self._embedding_backend = embedding_backend
        self._snapshot_dir = snapshot_dir
        self._target_vault_name = target_vault_name

        # IDs are TENTATIVE at construction. ``import_snapshot()`` resolves
        # them by looking up an existing in-progress import for the same
        # source path (resume semantics, Decision 10) before committing.
        self._import_id: UUID = uuid4()
        self._target_vault_id: UUID = uuid4()
        self._resumed: bool = False
        self._resume_state: str | None = None
        self._manifest: SnapshotManifestImport | None = None
        self._vault: VaultImport | None = None

    @property
    def import_id(self) -> UUID:
        return self._import_id

    @property
    def target_vault_id(self) -> UUID:
        return self._target_vault_id

    async def import_snapshot(self) -> UUID:
        """Run all phases. Returns ``target_vault_id`` once Phase E commits.

        Resume semantics (Decision 10): if a previous import for the same
        source path is recorded as in-progress, re-use its IDs and skip
        already-completed phases. If recorded as ``complete``, refuse.
        """
        self._snapshot_dir = _resolve_snapshot_dir(self._snapshot_dir)
        await self._validate_preflight()
        try:
            # Idempotent resume — skip phases that have already committed.
            # Note: do NOT unconditionally write state='staging' here; on
            # resume from a higher state (`db_committed`/`assets_committed`/
            # `embedded`) that would clobber the persisted progress, and a
            # subsequent crash would force re-running already-committed
            # phases against existing rows (PK collision in Phase B).
            phases_done = self._phases_completed()
            if not self._resumed:
                # Fresh import: record initial 'staging' so a partial
                # Phase A leaves a discoverable row. Concurrency: two
                # processes can both observe a missing row in
                # _validate_preflight and race the INSERT. The UNIQUE
                # INDEX on source_snapshot_path makes one of them raise
                # IntegrityError. Treat that as "the other process won
                # the race" — re-look up its row and either resume
                # against it or refuse if it's already complete.
                try:
                    await _record_import_state(
                        self._session,
                        self._target_vault_id,
                        self._import_id,
                        self._snapshot_dir,
                        'staging',
                    )
                    await self._session.commit()
                except IntegrityError:
                    await self._session.rollback()
                    existing = await _lookup_existing_import(self._session, self._snapshot_dir)
                    if existing is None:
                        # Shouldn't happen — UNIQUE INDEX raised but the
                        # row isn't visible. Re-raise so the caller
                        # observes the underlying error instead of a
                        # silent infinite loop.
                        raise
                    existing_target, existing_import, existing_state = existing
                    if existing_state == 'complete':
                        _refuse_completed_import(existing_target, self._snapshot_dir)
                    self._target_vault_id = existing_target
                    self._import_id = existing_import
                    self._resumed = True
                    self._resume_state = existing_state
                    phases_done = self._phases_completed()
                    logger.info(
                        'Concurrent import detected for %s; resuming peer at state=%s '
                        '(target_vault_id=%s, import_id=%s)',
                        self._snapshot_dir,
                        existing_state,
                        existing_target,
                        existing_import,
                    )

            if 'A' not in phases_done:
                await self._phase_a_stage_assets()
            if 'B' not in phases_done:
                await self._phase_b_db_transaction()
            if 'C' not in phases_done:
                await self._phase_c_commit_assets()
            if 'D' not in phases_done:
                await self._phase_d_embeddings_and_reindex()
            await self._phase_e_mark_complete()
            return self._target_vault_id
        except Exception:
            # Best-effort staging cleanup. Skipped on resume — the original
            # staging dir may still hold uncommitted assets we want to retry.
            if not self._resumed:
                await self._cleanup_staging()
            raise

    def _phases_completed(self) -> set[str]:
        """Map ``eval_import_state.state`` to the phases already past commit."""
        state = self._resume_state
        if state is None:
            return set()
        # State is set after the phase commits, so observing 'db_committed'
        # means Phase B finished; assets_committed means Phase C; etc.
        if state == 'db_committed':
            # Phase A may or may not have happened — assets staged is a
            # local FileStore op, not gated by a DB row. Re-running Phase A
            # is idempotent (FileStore.save uses save-or-overwrite).
            return {'A', 'B'}
        if state == 'assets_committed':
            return {'A', 'B', 'C'}
        if state == 'embedded':
            return {'A', 'B', 'C', 'D'}
        # 'staging' or 'complete' — staging means nothing past Phase A
        # committed; complete is rejected before we get here.
        return set()

    # ------------------------------------------------------------------
    # Preflight

    async def _validate_preflight(self) -> None:
        manifest = await _read_manifest(self._snapshot_dir)
        _check_version(manifest)
        _check_observation_schema(manifest)
        _check_embedding_model(manifest, self._embedding_backend)
        await _check_alembic_head(self._session, manifest)

        vault_raw = (self._snapshot_dir / 'vault.json').read_text(encoding='utf-8')
        vault = VaultImport.model_validate_json(vault_raw)
        _check_vault_not_global(vault)

        # Resume / refuse — keyed on source_snapshot_path (Decision 10/20).
        # Fresh target_vault_id was allocated in __init__; if a prior import
        # for this source path exists, replace tentative IDs with the
        # recorded ones so Phase B can re-use them on resume.
        existing = await _lookup_existing_import(self._session, self._snapshot_dir)
        if existing is not None:
            existing_target, existing_import, existing_state = existing
            if existing_state == 'complete':
                _refuse_completed_import(existing_target, self._snapshot_dir)
            self._target_vault_id = existing_target
            self._import_id = existing_import
            self._resumed = True
            self._resume_state = existing_state
            logger.info(
                'Resuming snapshot import for %s at state=%s (target_vault_id=%s, import_id=%s)',
                self._snapshot_dir,
                existing_state,
                existing_target,
                existing_import,
            )
        # Vault-name pre-check applies on both fresh and resume paths:
        # - Fresh: any existing vault with the same name blocks Phase B.
        # - Resume from 'staging': Phase B hasn't inserted yet; same blocker.
        # - Resume from 'db_committed' or later: the target row already
        #   exists with this name and target_vault_id, so it's allowed.
        await self._refuse_on_existing_vault_name()

        self._manifest = manifest
        self._vault = vault

    async def _refuse_on_existing_vault_name(self) -> None:
        result = await self._session.execute(
            select(Vault.id).where(Vault.name == self._target_vault_name)
        )
        existing_id = result.scalar_one_or_none()
        # Allow the resume case where Phase B has already inserted our own
        # target row.
        if existing_id is not None and existing_id != self._target_vault_id:
            raise SnapshotImportRefused(
                f'Vault name {self._target_vault_name!r} already exists '
                f'(id={existing_id}). Pass a unique target_vault_name or '
                f'delete the existing vault first.'
            )

    # ------------------------------------------------------------------
    # Phase A — FileStore staging

    @property
    def _staging_prefix(self) -> str:
        return f'_staging/{self._import_id}'

    def _final_asset_key(self, basename: str) -> str:
        return f'vault-{self._target_vault_id}/assets/{basename}'

    def _final_filestore_key(self, note_id: UUID) -> str:
        return f'vault-{self._target_vault_id}/notes/{note_id}/content.bin'

    def _staging_asset_key(self, basename: str) -> str:
        return f'{self._staging_prefix}/assets/{basename}'

    def _staging_filestore_key(self, note_id: UUID) -> str:
        return f'{self._staging_prefix}/notes/{note_id}/content.bin'

    async def _phase_a_stage_assets(self) -> None:
        if self._filestore is None:
            logger.info('No FileStore configured; skipping Phase A.')
            return
        notes_dir = self._snapshot_dir / 'notes'
        if not notes_dir.exists():
            return
        for note_dir in sorted(notes_dir.iterdir()):
            if not note_dir.is_dir():
                continue
            assets_dir = note_dir / 'assets'
            if assets_dir.exists():
                for asset_path in sorted(assets_dir.iterdir()):
                    if not asset_path.is_file():
                        continue
                    data = asset_path.read_bytes()
                    await self._filestore.save(self._staging_asset_key(asset_path.name), data)
            content_blob = note_dir / 'content.bin'
            if content_blob.is_file():
                # We need the note's UUID to build the staging key. The
                # metadata lives next to it.
                meta_raw = (note_dir / 'metadata.json').read_text(encoding='utf-8')
                note_id = UUID(json.loads(meta_raw)['id'])
                data = content_blob.read_bytes()
                await self._filestore.save(self._staging_filestore_key(note_id), data)
        logger.info('Phase A complete: staged assets under %s', self._staging_prefix)

    # ------------------------------------------------------------------
    # Phase B — DB transaction

    async def _phase_b_db_transaction(self) -> None:
        assert self._vault is not None
        # Raise statement_timeout for this connection — bulk inserts on
        # large vaults can exceed the 30s default. Reset in finally.
        conn = await self._session.connection()
        try:
            await conn.execute(text("SET statement_timeout = '5min'"))
            await self._insert_vault()
            await self._insert_entities_and_aliases()
            note_ids = await self._insert_notes()
            await self._insert_note_appends(note_ids)
            await self._insert_chunks()
            await self._insert_nodes()
            await self._insert_memory_units()
            await self._insert_unit_entities()
            await self._insert_entity_cooccurrences()
            await self._insert_mental_models()
            await self._insert_memory_links()
            await self._insert_vault_summary()
            await self._insert_governance()
            await _record_import_state(
                self._session,
                self._target_vault_id,
                self._import_id,
                self._snapshot_dir,
                'db_committed',
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        finally:
            try:
                await conn.execute(text("SET statement_timeout = '30s'"))
            except Exception:
                # Connection may already be closed/aborted; pool will reset.
                pass

    async def _insert_vault(self) -> None:
        assert self._vault is not None
        await self._session.execute(
            sa_insert(Vault.__table__).values(  # type: ignore[arg-type]
                id=self._target_vault_id,
                name=self._target_vault_name,
                description=self._vault.description,
                mw_mode=self._vault.mw_mode,
                created_at=self._vault.created_at,
            )
        )

    async def _insert_entities_and_aliases(self) -> None:
        derived = self._snapshot_dir / 'derived'
        # Materialize entities once so we can batch the canonical_name
        # cross-id collision pre-check. ``Entity.canonical_name`` carries a
        # UNIQUE constraint (idx_entities_canonical_name_unique); Postgres
        # supports only one ON CONFLICT target, so we refuse on collision
        # with a clear error rather than letting Phase B raise a raw
        # IntegrityError.
        entities: list[EntityImport] = [
            EntityImport.model_validate(raw)
            for raw in _read_jsonl_lines(derived / 'entities.jsonl')
        ]
        if entities:
            names = {e.canonical_name: e.id for e in entities}
            existing = await self._session.execute(
                select(Entity.id, Entity.canonical_name).where(
                    Entity.canonical_name.in_(list(names.keys()))  # type: ignore[attr-defined]
                )
            )
            for row_id, row_name in existing.all():
                snapshot_id = names[row_name]
                if row_id != snapshot_id:
                    raise SnapshotImportRefused(
                        f'Entity canonical_name {row_name!r} already exists with '
                        f'id={row_id} on the importing DB; snapshot has id={snapshot_id}. '
                        f'Eval testcontainers must start empty — drop the conflicting '
                        f'entity or import into a fresh DB.'
                    )
        for entity in entities:
            stmt = pg_insert(Entity.__table__).values(  # type: ignore[arg-type]
                id=entity.id,
                canonical_name=entity.canonical_name,
                phonetic_code=entity.phonetic_code,
                entity_type=entity.entity_type,
                first_seen=entity.first_seen,
                last_seen=entity.last_seen,
                mention_count=entity.mention_count,
                retrieval_count=entity.retrieval_count,
                last_retrieved_at=entity.last_retrieved_at,
            )
            await self._session.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        for raw in _read_jsonl_lines(derived / 'entity_aliases.jsonl'):
            alias = EntityAliasImport.model_validate(raw)
            stmt = pg_insert(EntityAlias.__table__).values(  # type: ignore[arg-type]
                id=alias.id,
                canonical_id=alias.canonical_id,
                name=alias.name,
                phonetic_code=alias.phonetic_code,
            )
            await self._session.execute(stmt.on_conflict_do_nothing(index_elements=['id']))

    async def _insert_notes(self) -> list[UUID]:
        notes_dir = self._snapshot_dir / 'notes'
        ids: list[UUID] = []
        if not notes_dir.exists():
            return ids
        for note_dir in sorted(notes_dir.iterdir()):
            if not note_dir.is_dir():
                continue
            meta_raw = (note_dir / 'metadata.json').read_text(encoding='utf-8')
            note = NoteImport.model_validate_json(meta_raw)
            note_md = note_dir / 'note.md'
            if note_md.exists():
                original_text: str | None = note_md.read_text(encoding='utf-8')
            else:
                original_text = None
            # Verify content_hash if present — defends against tampered/
            # corrupted note.md. Mismatch is treated as a hard refusal so
            # silent corruption can't propagate into eval results.
            if note.content_hash and original_text is not None:
                actual = hashlib.md5(original_text.encode('utf-8')).hexdigest()
                if actual != note.content_hash:
                    raise SnapshotImportError(
                        f'note.md content_hash mismatch for note {note.id}: '
                        f'expected={note.content_hash} actual={actual}. '
                        f'Snapshot may be corrupted or tampered.'
                    )
            # Rewrite asset paths on the row to point to the FINAL FileStore
            # keys (Phase C will move bytes into place before retrieval is
            # allowed; Phase E gates).
            rewritten_assets: list[str] = []
            for asset_rel in note.assets:
                basename = Path(asset_rel).name
                rewritten_assets.append(self._final_asset_key(basename))
            rewritten_filestore = (
                self._final_filestore_key(note.id) if note.filestore_path else None
            )
            await self._session.execute(
                sa_insert(Note.__table__).values(  # type: ignore[arg-type]
                    {
                        'id': note.id,
                        'vault_id': self._target_vault_id,
                        'session_id': note.session_id,
                        'title': note.title,
                        'description': note.description,
                        'original_text': original_text,
                        'page_index': note.page_index,
                        'content_hash': note.content_hash,
                        'filestore_path': rewritten_filestore,
                        'assets': rewritten_assets,
                        # SQL column name is 'metadata' (attr: doc_metadata).
                        'metadata': note.doc_metadata,
                        'publish_date': note.publish_date,
                        'status': note.status,
                        'superseded_by': note.superseded_by,
                        'appended_to': note.appended_to,
                        'summary_version_incorporated': note.summary_version_incorporated,
                        'created_at': note.created_at,
                        'updated_at': note.updated_at,
                    }
                )
            )
            ids.append(note.id)
        return ids

    async def _insert_note_appends(self, note_ids: list[UUID]) -> None:
        path = self._snapshot_dir / 'derived' / 'note_appends.jsonl'
        valid_ids = set(note_ids)
        for raw in _read_jsonl_lines(path):
            ap = NoteAppendImport.model_validate(raw)
            if ap.note_id not in valid_ids:
                raise SnapshotImportError(
                    f'NoteAppend {ap.append_id} references missing note {ap.note_id}'
                )
            await self._session.execute(
                sa_insert(NoteAppend.__table__).values(  # type: ignore[arg-type]
                    append_id=ap.append_id,
                    note_id=ap.note_id,
                    delta_sha256=ap.delta_sha256,
                    delta_bytes=ap.delta_bytes,
                    joiner=ap.joiner,
                    resulting_content_hash=ap.resulting_content_hash,
                    new_unit_ids=ap.new_unit_ids,
                    applied_at=ap.applied_at,
                )
            )

    async def _insert_chunks(self) -> None:
        path = self._snapshot_dir / 'derived' / 'chunks.jsonl'
        for raw in _read_jsonl_lines(path):
            ch = ChunkImport.model_validate(raw)
            await self._session.execute(
                sa_insert(Chunk.__table__).values(  # type: ignore[arg-type]
                    id=ch.id,
                    vault_id=self._target_vault_id,
                    note_id=ch.note_id,
                    text=ch.text,
                    content_hash=ch.content_hash,
                    status=ch.status,
                    embedding=None,
                    chunk_index=ch.chunk_index,
                    summary=ch.summary,
                    summary_formatted=ch.summary_formatted,
                    created_at=ch.created_at,
                )
            )

    async def _insert_nodes(self) -> None:
        path = self._snapshot_dir / 'derived' / 'nodes.jsonl'
        for raw in _read_jsonl_lines(path):
            n = NodeImport.model_validate(raw)
            await self._session.execute(
                sa_insert(Node.__table__).values(  # type: ignore[arg-type]
                    id=n.id,
                    vault_id=self._target_vault_id,
                    note_id=n.note_id,
                    block_id=n.block_id,
                    node_hash=n.node_hash,
                    title=n.title,
                    text=n.text,
                    summary=n.summary,
                    summary_formatted=n.summary_formatted,
                    level=n.level,
                    seq=n.seq,
                    token_estimate=n.token_estimate,
                    status=n.status,
                    created_at=n.created_at,
                )
            )

    async def _insert_memory_units(self) -> None:
        path = self._snapshot_dir / 'derived' / 'memory_units.jsonl'
        for raw in _read_jsonl_lines(path):
            _check_no_nan_floats(raw.get('confidence'), where='memory_units.confidence')
            _check_no_nan_floats(raw.get('importance'), where='memory_units.importance')
            _check_no_nan_floats(raw.get('stability'), where='memory_units.stability')
            u = MemoryUnitImport.model_validate(raw)
            await self._session.execute(
                sa_insert(MemoryUnit.__table__)
                .values(  # type: ignore[arg-type]
                    id=u.id,
                    vault_id=self._target_vault_id,
                    note_id=u.note_id,
                    chunk_id=u.chunk_id,
                    text=u.text,
                    fact_type=u.fact_type,
                    status=u.status,
                    embedding=None,
                    event_date=u.event_date,
                    occurred_start=u.occurred_start,
                    occurred_end=u.occurred_end,
                    mentioned_at=u.mentioned_at,
                    context=u.context,
                    is_deprioritized=u.is_deprioritized,
                    success_co_count=u.success_co_count,
                    failure_co_count=u.failure_co_count,
                    intent_class=u.intent_class,
                    risk_class=u.risk_class,
                    confidence=u.confidence,
                    confidence_evidence_count=u.confidence_evidence_count,
                    importance=u.importance,
                    stability=u.stability,
                    last_outcome_at=u.last_outcome_at,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                )
                .values({'metadata': u.unit_metadata})  # SQL col name (attr: unit_metadata)
            )

    async def _insert_unit_entities(self) -> None:
        path = self._snapshot_dir / 'derived' / 'unit_entities.jsonl'
        for raw in _read_jsonl_lines(path):
            ue = UnitEntityImport.model_validate(raw)
            await self._session.execute(
                sa_insert(UnitEntity.__table__).values(  # type: ignore[arg-type]
                    unit_id=ue.unit_id,
                    entity_id=ue.entity_id,
                    vault_id=self._target_vault_id,
                    success_co_count=ue.success_co_count,
                    failure_co_count=ue.failure_co_count,
                )
            )

    async def _insert_entity_cooccurrences(self) -> None:
        path = self._snapshot_dir / 'derived' / 'entity_cooccurrences.jsonl'
        for raw in _read_jsonl_lines(path):
            co = EntityCooccurrenceImport.model_validate(raw)
            await self._session.execute(
                sa_insert(EntityCooccurrence.__table__).values(  # type: ignore[arg-type]
                    entity_id_1=co.entity_id_1,
                    entity_id_2=co.entity_id_2,
                    vault_id=self._target_vault_id,
                    cooccurrence_count=co.cooccurrence_count,
                    last_cooccurred=co.last_cooccurred,
                    valid_from=co.valid_from,
                    valid_to=co.valid_to,
                )
            )

    async def _insert_mental_models(self) -> None:
        path = self._snapshot_dir / 'derived' / 'mental_models.jsonl'
        for raw in _read_jsonl_lines(path):
            mm = MentalModelImport.model_validate(raw)
            # Sanity-check observations against the frozen v1 schema.
            for obs in mm.observations:
                try:
                    ObservationV1.model_validate(obs)
                except Exception as e:
                    logger.warning(
                        'MentalModel %s observation failed v1 schema check: %s', mm.id, e
                    )
            await self._session.execute(
                sa_insert(MentalModel.__table__).values(  # type: ignore[arg-type]
                    id=mm.id,
                    vault_id=self._target_vault_id,
                    entity_id=mm.entity_id,
                    name=mm.name,
                    observations=mm.observations,
                    entity_metadata=mm.entity_metadata,
                    last_refreshed=mm.last_refreshed,
                    version=mm.version,
                    embedding=None,
                    success_co_count=mm.success_co_count,
                    failure_co_count=mm.failure_co_count,
                )
            )

    async def _insert_memory_links(self) -> None:
        path = self._snapshot_dir / 'derived' / 'memory_links.jsonl'
        for raw in _read_jsonl_lines(path):
            ml = MemoryLinkImport.model_validate(raw)
            await self._session.execute(
                sa_insert(MemoryLink.__table__).values(  # type: ignore[arg-type]
                    from_unit_id=ml.from_unit_id,
                    to_unit_id=ml.to_unit_id,
                    link_type=ml.link_type,
                    vault_id=self._target_vault_id,
                    entity_id=ml.entity_id,
                    link_metadata=ml.link_metadata,
                    weight=ml.weight,
                    created_at=ml.created_at,
                )
            )

    async def _insert_vault_summary(self) -> None:
        path = self._snapshot_dir / 'derived' / 'vault_summary.json'
        if not path.exists():
            return
        raw = path.read_text(encoding='utf-8')
        vs = VaultSummaryImport.model_validate_json(raw)
        await self._session.execute(
            sa_insert(VaultSummary.__table__).values(  # type: ignore[arg-type]
                id=vs.id,
                vault_id=self._target_vault_id,
                narrative=vs.narrative,
                themes=vs.themes,
                inventory=vs.inventory,
                key_entities=vs.key_entities,
                version=vs.version,
                notes_incorporated=vs.notes_incorporated,
                patch_log=vs.patch_log,
                needs_regeneration=vs.needs_regeneration,
                created_at=vs.created_at,
                updated_at=vs.updated_at,
            )
        )

    async def _insert_governance(self) -> None:
        gov = self._snapshot_dir / 'governance'
        for raw in _read_jsonl_lines(gov / 'maintenance_proposals.jsonl'):
            mp = MaintenanceProposalImport.model_validate(raw)
            await self._session.execute(
                sa_insert(MaintenanceProposal.__table__).values(  # type: ignore[arg-type]
                    id=mp.id,
                    vault_id=self._target_vault_id,
                    lint_type=mp.lint_type,
                    target_type=mp.target_type,
                    target_id=mp.target_id,
                    rule_name=mp.rule_name,
                    evidence=mp.evidence,
                    suggested_action=mp.suggested_action,
                    status=mp.status,
                    source=mp.source,
                    created_at=mp.created_at,
                    resolved_at=mp.resolved_at,
                    resolved_by=mp.resolved_by,
                )
            )
        for raw in _read_jsonl_lines(gov / 'procedure_outcomes.jsonl'):
            po = ProcedureOutcomeImport.model_validate(raw)
            await self._session.execute(
                sa_insert(ProcedureOutcome.__table__).values(  # type: ignore[arg-type]
                    id=po.id,
                    vault_id=self._target_vault_id,
                    kv_key=po.kv_key,
                    success_co_count=po.success_co_count,
                    failure_co_count=po.failure_co_count,
                    last_outcome_at=po.last_outcome_at,
                    created_at=po.created_at,
                    updated_at=po.updated_at,
                )
            )

    # ------------------------------------------------------------------
    # Phase C — FileStore commit

    async def _phase_c_commit_assets(self) -> None:
        if self._filestore is None:
            await _record_import_state(
                self._session,
                self._target_vault_id,
                self._import_id,
                self._snapshot_dir,
                'assets_committed',
            )
            await self._session.commit()
            return

        notes_dir = self._snapshot_dir / 'notes'
        if notes_dir.exists():
            for note_dir in sorted(notes_dir.iterdir()):
                if not note_dir.is_dir():
                    continue
                assets_dir = note_dir / 'assets'
                if assets_dir.exists():
                    for asset_path in sorted(assets_dir.iterdir()):
                        if not asset_path.is_file():
                            continue
                        src = self._staging_asset_key(asset_path.name)
                        dst = self._final_asset_key(asset_path.name)
                        await self._move_filestore_key(src, dst)
                content_blob = note_dir / 'content.bin'
                if content_blob.is_file():
                    meta_raw = (note_dir / 'metadata.json').read_text(encoding='utf-8')
                    note_id = UUID(json.loads(meta_raw)['id'])
                    src = self._staging_filestore_key(note_id)
                    dst = self._final_filestore_key(note_id)
                    await self._move_filestore_key(src, dst)
        # Sweep the empty staging prefix.
        try:
            await self._filestore.delete(self._staging_prefix, recursive=True)
        except Exception as e:
            logger.warning('Phase C: failed to delete staging prefix: %s', e)

        # Record state ONCE at the end of Phase C. The previous arrangement
        # had this block buried inside `_move_filestore_key`, which meant
        # an empty Phase C (no assets) never recorded `assets_committed`,
        # and a multi-asset Phase C wrote the row N times.
        await _record_import_state(
            self._session,
            self._target_vault_id,
            self._import_id,
            self._snapshot_dir,
            'assets_committed',
        )
        await self._session.commit()

    async def _move_filestore_key(self, src: str, dst: str) -> None:
        """Copy ``src`` -> ``dst`` then delete ``src`` on the FileStore.

        ``BaseAsyncFileStore.move_file`` calls ``_cp_file`` directly without
        creating the destination's parent directory for the file (non-dir)
        case. ``save`` does create parents. Round-trip via load+save+delete
        sidesteps the gap and works uniformly across local / S3 / GCS
        backends. Idempotent: missing-src is treated as already moved.

        Atomicity caveat: copy-then-delete is not atomic on any backend.
        If `delete` fails after `save` succeeds, the asset exists at both
        keys; Phase C's recursive sweep of `self._staging_prefix` collects
        the orphan src on the next pass.
        """
        assert self._filestore is not None
        if not await self._filestore.exists(src):
            return
        data = await self._filestore.load(src)
        await self._filestore.save(dst, data)
        await self._filestore.delete(src)

    # ------------------------------------------------------------------
    # Phase D — Embeddings + REINDEX

    async def _phase_d_embeddings_and_reindex(self) -> None:
        """Regenerate embeddings using the EXACT same text-formatting the
        extraction pipeline uses, so retrieval over an imported vault is
        bit-comparable to retrieval over the source.

        - ``Chunk.embedding``: raw ``Chunk.text`` (extraction at
          ``memory/extraction/engine.py:396-401`` — no ``format_for_embedding``).
        - ``MemoryUnit.embedding``: ``format_for_embedding(text=fact_text,
          fact_type, context)`` per ``embedding_processor.format_facts_for_embedding``.
        - ``MentalModel.embedding``: ``format_for_embedding(text=obs_text,
          fact_type='observation', context=mental_model.name)`` where
          ``obs_text = ' '.join(f'{title} - {content}' for o in observations)``
          per ``reflect/reflection.py:_phase_5_finalize`` line 346-353.

        Diverging here would silently invalidate retrieval scores. Tests
        assert that the EXACT extraction-time formatter is used.
        """
        embedder = await get_embedding_model(self._embedding_backend)

        # Chunks: raw text (no formatter), no `updated_at` column.
        await self._embed_chunks(embedder)
        # Memory units: format_for_embedding(text, fact_type, context).
        # Preserve `updated_at` verbatim (Decision 3) — the column's
        # `onupdate=func.now()` (memory/mixins.py:28) fires on UPDATE and
        # would otherwise trash the snapshot timestamp.
        await self._embed_memory_units(embedder)
        # MentalModels: ' '.join(title - content) → format_for_embedding(.., 'observation', name).
        await self._embed_mental_models(embedder)

        await self._reindex_hnsw()
        await _record_import_state(
            self._session,
            self._target_vault_id,
            self._import_id,
            self._snapshot_dir,
            'embedded',
        )
        await self._session.commit()

    async def _embed_chunks(self, embedder: Any) -> None:
        result = await self._session.execute(
            select(Chunk.id, Chunk.text).where(
                Chunk.vault_id == self._target_vault_id,
                Chunk.embedding.is_(None),  # type: ignore[union-attr]
            )
        )
        rows = list(result.all())
        if not rows:
            return
        for start in range(0, len(rows), EMBED_BATCH_SIZE):
            batch = rows[start : start + EMBED_BATCH_SIZE]
            texts = [(r[1] or '') for r in batch]
            vectors = embedder.encode(texts)
            for (row_id, _text), vec in zip(batch, vectors, strict=True):
                await self._session.execute(
                    update(Chunk)
                    .where(Chunk.id == row_id)
                    .values({Chunk.embedding: list(map(float, vec))})
                )
            await self._session.commit()

    async def _embed_memory_units(self, embedder: Any) -> None:
        result = await self._session.execute(
            select(
                MemoryUnit.id,
                MemoryUnit.text,
                MemoryUnit.fact_type,
                MemoryUnit.context,
                MemoryUnit.updated_at,
            ).where(
                MemoryUnit.vault_id == self._target_vault_id,
                MemoryUnit.embedding.is_(None),  # type: ignore[union-attr]
            )
        )
        rows = list(result.all())
        if not rows:
            return
        for start in range(0, len(rows), EMBED_BATCH_SIZE):
            batch = rows[start : start + EMBED_BATCH_SIZE]
            # `fact_type` is a Postgres TEXT column hydrated as plain str by
            # SQLModel/SA, so use it as-is. Avoid `str(...)` wrap: if a future
            # change ever hydrates as an enum, str(EnumMember) returns
            # `'FactTypes.WORLD'` not `'world'`, and `format_for_embedding`'s
            # `.capitalize()` would silently emit `'Facttypes.world'` —
            # silently corrupting the embedding format.
            texts = [
                format_for_embedding(
                    text=(r[1] or ''),
                    fact_type=coerce_enum_value(r[2]),
                    context=r[3],
                )
                for r in batch
            ]
            vectors = embedder.encode(texts)
            for (row_id, _t, _f, _c, uat), vec in zip(batch, vectors, strict=True):
                await self._session.execute(
                    update(MemoryUnit)
                    .where(MemoryUnit.id == row_id)
                    .values(
                        {
                            MemoryUnit.embedding: list(map(float, vec)),
                            MemoryUnit.updated_at: uat,
                        }
                    )
                )
            await self._session.commit()

    async def _embed_mental_models(self, embedder: Any) -> None:
        result = await self._session.execute(
            select(
                MentalModel.id,
                MentalModel.name,
                MentalModel.observations,
            ).where(
                MentalModel.vault_id == self._target_vault_id,
                MentalModel.embedding.is_(None),  # type: ignore[union-attr]
            )
        )
        rows = list(result.all())
        if not rows:
            return
        ids: list[UUID] = []
        texts: list[str] = []
        for row_id, name, observations in rows:
            if not observations:
                continue
            obs_text = ' '.join(
                f'{obs.get("title", "")} - {obs.get("content", "")}'
                for obs in observations
                if isinstance(obs, dict)
            ).strip()
            if not obs_text:
                continue
            ids.append(row_id)
            texts.append(format_for_embedding(text=obs_text, fact_type='observation', context=name))
        for start in range(0, len(ids), EMBED_BATCH_SIZE):
            chunk_ids = ids[start : start + EMBED_BATCH_SIZE]
            chunk_texts = texts[start : start + EMBED_BATCH_SIZE]
            vectors = embedder.encode(chunk_texts)
            for rid, vec in zip(chunk_ids, vectors, strict=True):
                await self._session.execute(
                    update(MentalModel)
                    .where(MentalModel.id == rid)
                    .values({MentalModel.embedding: list(map(float, vec))})
                )
            await self._session.commit()

    async def _reindex_hnsw(self) -> None:
        # REINDEX cannot run inside a transaction block; commit first and
        # then issue each REINDEX on its own AUTOCOMMIT connection. Failures
        # are logged but non-fatal — eval results don't depend on HNSW
        # latency, only correctness, and a missed REINDEX just degrades
        # query latency.
        await self._session.commit()
        result = await self._session.execute(
            text(
                'SELECT indexname FROM pg_indexes '
                "WHERE schemaname = 'public' AND indexdef ILIKE '%USING hnsw%'"
            )
        )
        indexes = [r[0] for r in result.all()]
        await self._session.commit()

        engine = self._session.bind
        if engine is None:
            logger.warning('No engine bound to session; skipping REINDEX')
            return

        for idx in indexes:
            try:
                async with engine.connect() as conn:
                    autocommit = await conn.execution_options(isolation_level='AUTOCOMMIT')
                    try:
                        await autocommit.execute(text(f'REINDEX INDEX CONCURRENTLY "{idx}"'))
                    except Exception as e:
                        logger.warning(
                            'REINDEX CONCURRENTLY failed on %s: %s — falling back', idx, e
                        )
                        await autocommit.execute(text(f'REINDEX INDEX "{idx}"'))
            except Exception as e:
                logger.warning('REINDEX skipped for %s: %s', idx, e)

    # ------------------------------------------------------------------
    # Phase E — mark complete

    async def _phase_e_mark_complete(self) -> None:
        await _record_import_state(
            self._session,
            self._target_vault_id,
            self._import_id,
            self._snapshot_dir,
            'complete',
        )
        await self._session.commit()
        logger.info(
            'Snapshot import complete: target_vault_id=%s import_id=%s',
            self._target_vault_id,
            self._import_id,
        )

    # ------------------------------------------------------------------
    # Cleanup

    async def _cleanup_staging(self) -> None:
        if self._filestore is None:
            return
        try:
            await self._filestore.delete(self._staging_prefix, recursive=True)
        except Exception as e:
            logger.warning('Failed to clean staging prefix on error: %s', e)
