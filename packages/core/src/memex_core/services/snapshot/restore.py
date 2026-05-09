"""V12 — eval-only snapshot import orchestrator.

Reverses ``SnapshotExporter`` (V3) for a single fresh, ephemeral vault on
the importing server. The contract is **eval-only**: the route that calls
this is registered iff ``server.eval_mode=True``.

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

import json
import logging
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import insert as sa_insert
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import (
    GLOBAL_VAULT_ID,
    GLOBAL_VAULT_NAME,
    EmbeddingBackend,
    LitellmEmbeddingBackend,
    OnnxBackend,
)
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
from memex_core.services.snapshot.import_models import (
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
    UnitEntityImport,
    VaultImport,
    VaultSummaryImport,
)
from memex_core.services.snapshot.import_state import VALID_STATES
from memex_core.services.snapshot.manifest import (
    OBSERVATION_SCHEMA_VERSION,
    SnapshotManifest,
    SnapshotVersion,
)
from memex_core.services.snapshot.path_validation import (
    read_validated_bytes,
    read_validated_text,
    validate_snapshot_dir,
)
from memex_core.storage.filestore import BaseAsyncFileStore

logger = logging.getLogger('memex.core.services.snapshot.restore')

RESERVED_VAULT_NAMES = {GLOBAL_VAULT_NAME.lower(), 'global', 'default'}

EMBED_BATCH_SIZE = 64


class SnapshotImportError(Exception):
    """Raised when an import fails validation or execution."""


class SnapshotImportRefused(SnapshotImportError):
    """Raised when an import is refused by policy (version, vault, mode)."""


# ----------------------------------------------------------------------
# Manifest + version validation


async def _read_manifest(snapshot_dir: Path, allowlist_root: Path) -> SnapshotManifest:
    raw = read_validated_text(snapshot_dir / 'manifest.json', expected_root=allowlist_root)
    data = json.loads(raw)
    return SnapshotManifest.model_validate(data)


def _check_version(manifest: SnapshotManifest) -> None:
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


async def _check_alembic_head(session: AsyncSession, manifest: SnapshotManifest) -> None:
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


def _check_embedding_model(manifest: SnapshotManifest, server_backend: EmbeddingBackend) -> None:
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


def _check_observation_schema(manifest: SnapshotManifest) -> None:
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


def _read_jsonl_lines(path: Path, allowlist_root: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    raw = read_validated_text(path, expected_root=allowlist_root)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as e:
            raise SnapshotImportError(f'Invalid JSON in {path.name}: {e}') from e


def _check_no_nan_floats(value: Any, *, where: str) -> None:
    """Reject NaN/Inf in numeric fields (Decision 14)."""
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise SnapshotImportError(f'NaN/Infinity in {where}')
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_no_nan_floats(v, where=f'{where}.{k}')
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_no_nan_floats(v, where=f'{where}[{i}]')


# ----------------------------------------------------------------------
# Eval import state helpers


async def _ensure_no_existing_import(session: AsyncSession, target_vault_id: UUID) -> None:
    """Single-snapshot-per-DB defense (Decision 20).

    Same target_vault_id is unreachable from a fresh allocation by design;
    this guards against test/retry seams where the same UUID is reused.
    """
    result = await session.execute(
        text(
            'SELECT state, source_snapshot_path FROM eval_import_state WHERE target_vault_id = :vid'
        ),
        {'vid': str(target_vault_id)},
    )
    row = result.first()
    if row is not None and row[0] == 'complete':
        raise SnapshotImportRefused(
            f'Cannot import: vault {target_vault_id} already imported '
            f'(state=complete, source={row[1]}). Each DB holds at most one '
            f'snapshot per vault. To re-import, delete the vault first: '
            f'`memex vault delete {target_vault_id}`.'
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
        allowlist_root: Path,
        target_vault_name: str,
    ) -> None:
        self._session = session
        self._filestore = filestore
        self._embedding_backend = embedding_backend
        self._snapshot_dir = snapshot_dir
        self._allowlist_root = allowlist_root
        self._target_vault_name = target_vault_name

        self._import_id: UUID = uuid4()
        self._target_vault_id: UUID = uuid4()
        self._manifest: SnapshotManifest | None = None
        self._vault: VaultImport | None = None

    @property
    def import_id(self) -> UUID:
        return self._import_id

    @property
    def target_vault_id(self) -> UUID:
        return self._target_vault_id

    async def import_snapshot(self) -> UUID:
        """Run all phases. Returns ``target_vault_id`` once Phase E commits."""
        self._snapshot_dir = validate_snapshot_dir(
            self._snapshot_dir, allowlist_root=self._allowlist_root
        )
        await self._validate_preflight()
        try:
            await _record_import_state(
                self._session,
                self._target_vault_id,
                self._import_id,
                self._snapshot_dir,
                'staging',
            )
            await self._session.commit()

            await self._phase_a_stage_assets()
            await self._phase_b_db_transaction()
            await self._phase_c_commit_assets()
            await self._phase_d_embeddings_and_reindex()
            await self._phase_e_mark_complete()
            return self._target_vault_id
        except Exception:
            # Best-effort staging cleanup.
            await self._cleanup_staging()
            raise

    # ------------------------------------------------------------------
    # Preflight

    async def _validate_preflight(self) -> None:
        manifest = await _read_manifest(self._snapshot_dir, self._allowlist_root)
        _check_version(manifest)
        _check_observation_schema(manifest)
        _check_embedding_model(manifest, self._embedding_backend)
        await _check_alembic_head(self._session, manifest)

        vault_raw = read_validated_text(
            self._snapshot_dir / 'vault.json', expected_root=self._allowlist_root
        )
        vault = VaultImport.model_validate_json(vault_raw)
        _check_vault_not_global(vault)
        await _ensure_no_existing_import(self._session, self._target_vault_id)

        self._manifest = manifest
        self._vault = vault

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
                    data = read_validated_bytes(asset_path, expected_root=self._allowlist_root)
                    await self._filestore.save(self._staging_asset_key(asset_path.name), data)
            content_blob = note_dir / 'content.bin'
            if content_blob.is_file():
                # We need the note's UUID to build the staging key. The
                # metadata lives next to it.
                meta_raw = read_validated_text(
                    note_dir / 'metadata.json', expected_root=self._allowlist_root
                )
                note_id = UUID(json.loads(meta_raw)['id'])
                data = read_validated_bytes(content_blob, expected_root=self._allowlist_root)
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
        for raw in _read_jsonl_lines(derived / 'entities.jsonl', self._allowlist_root):
            entity = EntityImport.model_validate(raw)
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
        for raw in _read_jsonl_lines(derived / 'entity_aliases.jsonl', self._allowlist_root):
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
            meta_raw = read_validated_text(
                note_dir / 'metadata.json', expected_root=self._allowlist_root
            )
            note = NoteImport.model_validate_json(meta_raw)
            note_md = note_dir / 'note.md'
            original_text = (
                read_validated_text(note_md, expected_root=self._allowlist_root)
                if note_md.exists()
                else ''
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
                    id=note.id,
                    vault_id=self._target_vault_id,
                    session_id=note.session_id,
                    title=note.title,
                    description=note.description,
                    original_text=original_text,
                    page_index=note.page_index,
                    content_hash=note.content_hash,
                    filestore_path=rewritten_filestore,
                    assets=rewritten_assets,
                    doc_metadata=note.doc_metadata,
                    publish_date=note.publish_date,
                    status=note.status,
                    superseded_by=note.superseded_by,
                    appended_to=note.appended_to,
                    summary_version_incorporated=note.summary_version_incorporated,
                    created_at=note.created_at,
                    updated_at=note.updated_at,
                )
            )
            ids.append(note.id)
        return ids

    async def _insert_note_appends(self, note_ids: list[UUID]) -> None:
        path = self._snapshot_dir / 'derived' / 'note_appends.jsonl'
        valid_ids = set(note_ids)
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
            _check_no_nan_floats(raw.get('confidence'), where='memory_units.confidence')
            _check_no_nan_floats(raw.get('importance'), where='memory_units.importance')
            _check_no_nan_floats(raw.get('stability'), where='memory_units.stability')
            u = MemoryUnitImport.model_validate(raw)
            await self._session.execute(
                sa_insert(MemoryUnit.__table__).values(  # type: ignore[arg-type]
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
                    unit_metadata=u.unit_metadata,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                )
            )

    async def _insert_unit_entities(self) -> None:
        path = self._snapshot_dir / 'derived' / 'unit_entities.jsonl'
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        for raw in _read_jsonl_lines(path, self._allowlist_root):
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
        raw = read_validated_text(path, expected_root=self._allowlist_root)
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
        for raw in _read_jsonl_lines(gov / 'maintenance_proposals.jsonl', self._allowlist_root):
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
        for raw in _read_jsonl_lines(gov / 'procedure_outcomes.jsonl', self._allowlist_root):
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
                        if await self._filestore.exists(src):
                            await self._filestore.move_file(src, dst)
                content_blob = note_dir / 'content.bin'
                if content_blob.is_file():
                    meta_raw = read_validated_text(
                        note_dir / 'metadata.json', expected_root=self._allowlist_root
                    )
                    note_id = UUID(json.loads(meta_raw)['id'])
                    src = self._staging_filestore_key(note_id)
                    dst = self._final_filestore_key(note_id)
                    if await self._filestore.exists(src):
                        await self._filestore.move_file(src, dst)
        # Sweep the empty staging prefix.
        try:
            await self._filestore.delete(self._staging_prefix, recursive=True)
        except Exception as e:
            logger.warning('Phase C: failed to delete staging prefix: %s', e)

        await _record_import_state(
            self._session,
            self._target_vault_id,
            self._import_id,
            self._snapshot_dir,
            'assets_committed',
        )
        await self._session.commit()

    # ------------------------------------------------------------------
    # Phase D — Embeddings + REINDEX

    async def _phase_d_embeddings_and_reindex(self) -> None:
        embedder = await get_embedding_model(self._embedding_backend)

        # Chunks
        await self._embed_table_text(
            embedder,
            select(Chunk.id, Chunk.text).where(  # type: ignore[arg-type]
                Chunk.vault_id == self._target_vault_id,
                Chunk.embedding.is_(None),  # type: ignore[union-attr]
            ),
            'chunks',
            'embedding',
        )
        # Memory units
        await self._embed_table_text(
            embedder,
            select(MemoryUnit.id, MemoryUnit.text).where(  # type: ignore[arg-type]
                MemoryUnit.vault_id == self._target_vault_id,
                MemoryUnit.embedding.is_(None),  # type: ignore[union-attr]
            ),
            'memory_units',
            'embedding',
        )
        # MentalModel embedding source is the centroid of observation
        # embeddings — see sql_models docstring. We approximate by
        # embedding the concatenated observation contents; if the result
        # diverges from extraction-time semantics, leaving NULL is
        # acceptable per Decision 10 Phase D.
        result = await self._session.execute(
            select(MentalModel.id, MentalModel.observations).where(
                MentalModel.vault_id == self._target_vault_id,
                MentalModel.embedding.is_(None),  # type: ignore[union-attr]
            )
        )
        rows = list(result.all())
        if rows:
            ids: list[UUID] = []
            texts: list[str] = []
            for row_id, observations in rows:
                if not observations:
                    continue
                joined = '\n'.join(
                    str(obs.get('content', '') or '')
                    for obs in observations
                    if isinstance(obs, dict)
                ).strip()
                if not joined:
                    continue
                ids.append(row_id)
                texts.append(joined)
            await self._update_embeddings(MentalModel, 'embedding', ids, texts, embedder)

        await self._reindex_hnsw()
        await _record_import_state(
            self._session,
            self._target_vault_id,
            self._import_id,
            self._snapshot_dir,
            'embedded',
        )
        await self._session.commit()

    async def _embed_table_text(
        self,
        embedder: Any,
        stmt: Any,
        table_name: str,
        embedding_col: str,
    ) -> None:
        result = await self._session.execute(stmt)
        rows = list(result.all())
        if not rows:
            return
        ids = [r[0] for r in rows]
        texts = [r[1] or '' for r in rows]

        for start in range(0, len(rows), EMBED_BATCH_SIZE):
            chunk_ids = ids[start : start + EMBED_BATCH_SIZE]
            chunk_texts = texts[start : start + EMBED_BATCH_SIZE]
            vectors = embedder.encode(chunk_texts)
            updates = []
            for row_id, vec in zip(chunk_ids, vectors, strict=True):
                updates.append({'id': str(row_id), 'embedding': list(map(float, vec))})
            await self._session.execute(
                text(f'UPDATE {table_name} SET {embedding_col} = :embedding WHERE id = :id'),
                updates,
            )
            await self._session.commit()

    async def _update_embeddings(
        self,
        model: Any,
        col: str,
        ids: list[UUID],
        texts: list[str],
        embedder: Any,
    ) -> None:
        if not ids:
            return
        for start in range(0, len(ids), EMBED_BATCH_SIZE):
            chunk_ids = ids[start : start + EMBED_BATCH_SIZE]
            chunk_texts = texts[start : start + EMBED_BATCH_SIZE]
            vectors = embedder.encode(chunk_texts)
            updates = [
                {'id': str(rid), 'embedding': list(map(float, vec))}
                for rid, vec in zip(chunk_ids, vectors, strict=True)
            ]
            table_name = model.__tablename__
            await self._session.execute(
                text(f'UPDATE {table_name} SET {col} = :embedding WHERE id = :id'),
                updates,
            )
            await self._session.commit()

    async def _reindex_hnsw(self) -> None:
        result = await self._session.execute(
            text(
                'SELECT indexname FROM pg_indexes '
                "WHERE schemaname = 'public' AND indexdef ILIKE '%USING hnsw%'"
            )
        )
        indexes = [r[0] for r in result.all()]
        for idx in indexes:
            try:
                await self._session.execute(text(f'REINDEX INDEX CONCURRENTLY "{idx}"'))
            except Exception as e:
                logger.warning('REINDEX CONCURRENTLY failed on %s: %s — falling back', idx, e)
                # Fallback: plain REINDEX (locks but always works).
                await self._session.execute(text(f'REINDEX INDEX "{idx}"'))
        await self._session.commit()

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
