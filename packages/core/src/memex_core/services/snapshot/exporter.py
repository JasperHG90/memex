"""Vault snapshot export orchestrator.

Walks a vault's tables in dependency order, writes JSONL/JSON files +
asset bytes to a local output directory, and stamps a manifest.json
last so partial failures don't look like completed exports.

Out of scope (per BACKLOG.md): import / restore (V12 lives in
memex_eval), incremental + watermarks, entity reconciliation,
target-vault rebinding.

The contract is enforced by Pydantic export models in ``export_models``:
a column added to a SQLModel does NOT silently leak — it forces an
explicit decision (add to the export model = MINOR bump on snapshot
SemVer, or document as excluded).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME
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
    EMBEDDING_DIMENSION,
)
from memex_core.services.snapshot.enum_coerce import coerce_enum_value
from memex_core.services.snapshot.export_models import (
    ChunkExport,
    EntityAliasExport,
    EntityCooccurrenceExport,
    EntityExport,
    MaintenanceProposalExport,
    MemoryLinkExport,
    MemoryUnitExport,
    MentalModelExport,
    NodeExport,
    NoteAppendExport,
    NoteExport,
    ProcedureOutcomeExport,
    UnitEntityExport,
    VaultExport,
    VaultSummaryExport,
)
from memex_core.services.snapshot.manifest import (
    EmbeddingModelIdentity,
    OBSERVATION_SCHEMA_VERSION,
    SnapshotManifest,
    SNAPSHOT_VERSION,
)
from memex_core.storage.filestore import BaseAsyncFileStore

logger = logging.getLogger('memex.core.services.snapshot')


# Reserved/forbidden vault names that may not be exported. Per Decision 13
# of the V12 plan: exporting the global vault is refused (V12 import would
# refuse it anyway, and shipping global state via this mechanism crosses
# the eval-only intent). Defended on both id and name to catch user
# mistakes ("I named my vault 'global'").
_FORBIDDEN_VAULT_NAMES = frozenset({GLOBAL_VAULT_NAME, 'global', 'default'})


class SnapshotExportError(RuntimeError):
    """Raised on any unrecoverable export failure."""


def _sanitize_for_dirname(value: str) -> str:
    """Reduce arbitrary text to a filesystem-safe directory fragment.

    Lowercase, replace non-[a-z0-9-_] runs with a single hyphen, trim
    leading/trailing hyphens, cap length to 80 chars. Empty result is
    returned as ``'untitled'``.
    """
    cleaned = re.sub(r'[^a-z0-9_-]+', '-', value.lower()).strip('-')
    if not cleaned:
        return 'untitled'
    return cleaned[:80]


def _normalize_zulu(value: Any) -> Any:
    """Recursively rewrite ``...Z`` ISO-8601 datetime strings to ``...+00:00``.

    Pydantic v2 ``mode='json'`` emits UTC datetimes with the trailing ``Z``
    short form; the snapshot contract (per V12 plan, JSONL canonical
    encoding) requires the explicit ``+00:00`` offset. We post-process the
    serialised payload rather than register a custom field serialiser on
    every export model to keep the contract enforced in one place.
    """
    if isinstance(value, str):
        if len(value) >= 20 and value.endswith('Z') and value[10] == 'T':
            return value[:-1] + '+00:00'
        return value
    if isinstance(value, dict):
        return {k: _normalize_zulu(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_zulu(v) for v in value]
    return value


def _to_jsonable(model: BaseModel) -> dict[str, Any]:
    """Pydantic ``model_dump`` with deterministic JSON-friendly output:

    - Datetimes serialised as ISO-8601 with ``+00:00`` (UTC) — never ``Z``.
    - UUIDs as lowercase canonical strings.
    - Floats: NaN / +-Infinity not produced by SQL backends; if encountered,
      ``json.dumps`` (allow_nan=False) below will fail loudly rather than
      emit JSON-incompatible tokens.
    """
    payload = model.model_dump(mode='json')
    return _normalize_zulu(payload)


def _dump_jsonl_line(model: BaseModel) -> str:
    return json.dumps(_to_jsonable(model), allow_nan=False, sort_keys=True, ensure_ascii=False)


class SnapshotExporter:
    """Orchestrates an export of a single vault into ``output_dir``.

    Callers MUST pass ``embedding_model`` constructed from the live server
    config. The registry default is only correct if the source server is
    using the built-in ONNX backend; if the server uses LiteLLM the
    registry-default fallback would write a manifest that lies about which
    embedding model produced the units in the snapshot, and V12 import
    would silently misclassify model identity.

    Usage::

        from memex_core.services.snapshot.manifest import EmbeddingModelIdentity
        from memex_core.memory.sql_models import EMBEDDING_DIMENSION

        identity = EmbeddingModelIdentity(
            name='<repo_id-or-litellm/<model>>',
            dim=EMBEDDING_DIMENSION,
            hash='<revision-or-empty>',
        )
        async with metastore.session() as session:
            exporter = SnapshotExporter(
                session=session,
                filestore=filestore,
                vault_id_or_name='my-vault',
                output_dir=Path('/tmp/snap'),
                embedding_model=identity,
            )
            await exporter.export()
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        filestore: BaseAsyncFileStore | None,
        vault_id_or_name: UUID | str,
        output_dir: Path,
        embedding_model: EmbeddingModelIdentity | None = None,
    ) -> None:
        self._session = session
        self._filestore = filestore
        self._vault_selector = vault_id_or_name
        self._output_dir = Path(output_dir)
        # Lazy-resolved
        self._vault: Vault | None = None
        self._table_counts: dict[str, int] = {}
        self._embedding_model = embedding_model

    # ------------------------------------------------------------------
    # Public entry point

    async def export(self) -> SnapshotManifest:
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Refuse if the directory looks like a previously-completed snapshot
        # — overwriting silently would corrupt JSONL counts vs. orphan files
        # on disk. Caller is expected to use a fresh dir or remove
        # manifest.json explicitly.
        if (self._output_dir / 'manifest.json').exists():
            raise SnapshotExportError(
                f'Refusing to overwrite existing snapshot at {self._output_dir}: '
                'manifest.json already present. Remove it (and its siblings) first.'
            )

        # In-progress marker. Importers (V12) refuse a snapshot dir that
        # carries this file; manifest-last-write is the success signal,
        # marker-removed-on-success is its sibling.
        marker = self._output_dir / '.exporting'
        marker.write_text(
            f'started_at={datetime.now(timezone.utc).isoformat()}\n', encoding='utf-8'
        )

        try:
            # Pin a snapshot of the DB at one isolation boundary so the
            # multi-query export observes a consistent point-in-time —
            # otherwise concurrent inserts/deletes between queries can
            # produce a torn export (e.g. a Note row but no chunks for it).
            # ``REPEATABLE READ`` is sufficient: within the transaction
            # all SELECTs see the snapshot taken at the first read.
            await self._session.execute(
                text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            )

            self._vault = await self._resolve_vault()
            self._refuse_global(self._vault)

            # Embedding-model identity. The caller is expected to construct
            # this from the live server config (see class docstring). When
            # absent we fall back to the registry default, which is ONLY
            # correct on a vanilla ONNX-backed server — log a loud warning
            # because the consumer has no other way to detect the lie.
            if self._embedding_model is None:
                logger.warning(
                    'SnapshotExporter invoked without explicit embedding_model; '
                    'falling back to MODEL_REGISTRY default. The manifest will be '
                    'incorrect if this server uses a non-ONNX embedding backend. '
                    'Pass embedding_model derived from your runtime config (see '
                    'class docstring).'
                )
                self._embedding_model = await self._resolve_embedding_identity()

            alembic_head = await self._read_alembic_head()
            exported_at = datetime.now(timezone.utc)

            # Order matters: write source-of-truth tables first, then derived,
            # then governance, then manifest LAST so a partial export is
            # detectable (no manifest.json = not complete).
            await self._write_vault_json()
            await self._write_notes_tree()
            await self._write_derived()
            await self._write_governance()

            manifest = SnapshotManifest(
                snapshot_version=SNAPSHOT_VERSION,
                source_vault_id=self._vault.id,
                source_vault_name=self._vault.name,
                exported_at=exported_at,
                alembic_head=alembic_head,
                embedding_model=self._embedding_model,
                observation_schema_version=OBSERVATION_SCHEMA_VERSION,
                table_counts=dict(self._table_counts),
            )
            self._write_json(self._output_dir / 'manifest.json', _to_jsonable(manifest))
            self._write_readme(manifest)
        finally:
            # Remove the marker on either success or failure. A crash
            # between marker-creation and this finally block leaves the
            # marker; that's the desired signal (a process death is
            # indistinguishable from a partial write, both are unsafe to
            # consume).
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
        return manifest

    # ------------------------------------------------------------------
    # Vault resolution + refusals

    async def _resolve_vault(self) -> Vault:
        sel = self._vault_selector
        if isinstance(sel, UUID):
            stmt = select(Vault).where(Vault.id == sel)
        else:
            stmt = select(Vault).where(Vault.name == sel)
        result = await self._session.execute(stmt)
        vault = result.scalar_one_or_none()
        if vault is None:
            raise SnapshotExportError(f'Vault not found: {sel!r}')
        return vault

    @staticmethod
    def _refuse_global(vault: Vault) -> None:
        if vault.id == GLOBAL_VAULT_ID:
            raise SnapshotExportError(
                f'Refusing to export the global vault (id={vault.id}). '
                'Eval-only snapshot tooling does not support global-vault export; '
                'use pg_dump for memex-to-memex transfer instead.'
            )
        if vault.name in _FORBIDDEN_VAULT_NAMES:
            raise SnapshotExportError(
                f'Refusing to export vault with reserved name {vault.name!r}; '
                f'reserved names: {sorted(_FORBIDDEN_VAULT_NAMES)}.'
            )

    # ------------------------------------------------------------------
    # Provenance helpers

    async def _read_alembic_head(self) -> str:
        result = await self._session.execute(text('SELECT version_num FROM alembic_version'))
        row = result.first()
        if row is None:
            raise SnapshotExportError(
                'alembic_version table is empty; cannot stamp snapshot manifest.'
            )
        return str(row[0])

    @staticmethod
    async def _resolve_embedding_identity() -> EmbeddingModelIdentity:
        from memex_core.memory.models.base import MODEL_REGISTRY

        spec = MODEL_REGISTRY['embedding']
        # repo_id is the canonical identifier. revision pins the model
        # version. We expose them as ``name`` and ``hash`` respectively;
        # if a future ONNX backend computes a content hash we can promote
        # that into ``hash``.
        return EmbeddingModelIdentity(
            name=str(spec.repo_id),
            dim=EMBEDDING_DIMENSION,
            hash=str(spec.revision),
        )

    # ------------------------------------------------------------------
    # Vault.json + notes tree

    async def _write_vault_json(self) -> None:
        assert self._vault is not None
        export = VaultExport(
            id=self._vault.id,
            name=self._vault.name,
            description=self._vault.description,
            mw_mode=coerce_enum_value(self._vault.mw_mode),
            created_at=self._vault.created_at,
        )
        self._write_json(self._output_dir / 'vault.json', _to_jsonable(export))
        self._table_counts['vaults'] = 1

    async def _write_notes_tree(self) -> None:
        assert self._vault is not None
        notes_dir = self._output_dir / 'notes'
        notes_dir.mkdir(exist_ok=True)
        result = await self._session.execute(select(Note).where(Note.vault_id == self._vault.id))
        notes = list(result.scalars())
        self._table_counts['notes'] = len(notes)

        for note in notes:
            note_dir_name = f'{_sanitize_for_dirname(note.title or "untitled")}_{note.id.hex[:8]}'
            note_dir = notes_dir / note_dir_name
            note_dir.mkdir(exist_ok=True)

            # Write the body as note.md so consumers can read content
            # without parsing the metadata JSON. Distinguish None
            # (column was NULL — no note.md file) from '' (column was
            # explicitly empty — note.md exists with zero bytes); V12
            # imports preserve the distinction.
            if note.original_text is not None:
                (note_dir / 'note.md').write_text(note.original_text, encoding='utf-8')

            # Asset rewriting. We copy bytes (from the FileStore) to
            # ``./assets/<basename>`` and rewrite the metadata fields.
            # Two assets with the same basename in different source dirs
            # would collide on the same target path, silently overwriting
            # each other; track basenames-in-use and disambiguate by
            # appending ``-<n>`` before the extension.
            rewritten_assets: list[str] = []
            assets_dir = note_dir / 'assets'
            used_basenames: set[str] = set()

            for asset_key in note.assets or []:
                rewritten = await self._copy_asset(asset_key, assets_dir, used_basenames)
                if rewritten is not None:
                    rewritten_assets.append(rewritten)

            # filestore_path is the source-document path inside the FileStore
            # (e.g. an uploaded PDF). Copy it as ``content.bin`` and rewrite.
            rewritten_filestore: str | None = None
            if note.filestore_path:
                rewritten_filestore = await self._copy_filestore_blob(note.filestore_path, note_dir)

            export = NoteExport(
                id=note.id,
                vault_id=note.vault_id,
                session_id=note.session_id,
                title=note.title,
                description=note.description,
                page_index=note.page_index,
                content_hash=note.content_hash,
                filestore_path=rewritten_filestore,
                assets=rewritten_assets,
                doc_metadata=note.doc_metadata or {},
                publish_date=note.publish_date,
                status=str(note.status),
                superseded_by=note.superseded_by,
                appended_to=note.appended_to,
                summary_version_incorporated=note.summary_version_incorporated,
                created_at=note.created_at,
                updated_at=note.updated_at,
            )
            self._write_json(note_dir / 'metadata.json', _to_jsonable(export))

    async def _copy_asset(
        self,
        asset_key: str,
        assets_dir: Path,
        used_basenames: set[str],
    ) -> str | None:
        """Copy one asset from the FileStore into ``assets_dir``.

        Returns the relative-to-snapshot path (``./assets/<basename>``).
        On failure (missing file, FileStore unavailable) logs a warning
        and returns None — the snapshot remains usable; the consumer
        will see a missing asset.

        Disambiguates basename collisions: if two source assets resolve
        to the same basename (e.g. ``a/diagram.png`` and ``b/diagram.png``)
        the second is renamed ``diagram-1.png``, the third
        ``diagram-2.png``, etc., so no asset is ever silently overwritten.
        """
        if self._filestore is None:
            logger.warning('No FileStore configured; skipping asset %s', asset_key)
            return None
        try:
            data = await self._filestore.load(asset_key)
        except Exception as e:
            logger.warning('Failed to load asset %s from FileStore: %s', asset_key, e)
            return None
        assets_dir.mkdir(exist_ok=True)
        # Use the basename to avoid leaking source-side directory layout.
        basename = Path(asset_key).name or asset_key
        unique = self._unique_basename(basename, used_basenames)
        used_basenames.add(unique)
        target = assets_dir / unique
        target.write_bytes(data)
        return f'./assets/{unique}'

    @staticmethod
    def _unique_basename(basename: str, used: set[str]) -> str:
        if basename not in used:
            return basename
        stem, dot, suffix = basename.rpartition('.')
        # Treat dotless names as a single stem.
        if not stem:
            stem, dot, suffix = basename, '', ''
        n = 1
        while True:
            candidate = f'{stem}-{n}{dot}{suffix}'
            if candidate not in used:
                return candidate
            n += 1

    async def _copy_filestore_blob(self, key: str, note_dir: Path) -> str | None:
        if self._filestore is None:
            logger.warning('No FileStore configured; skipping filestore_path %s', key)
            return None
        try:
            data = await self._filestore.load(key)
        except Exception as e:
            logger.warning('Failed to load filestore blob %s: %s', key, e)
            return None
        target = note_dir / 'content.bin'
        target.write_bytes(data)
        return './content.bin'

    # ------------------------------------------------------------------
    # Derived state (JSONL)

    async def _write_derived(self) -> None:
        assert self._vault is not None
        derived = self._output_dir / 'derived'
        derived.mkdir(exist_ok=True)
        vid = self._vault.id

        await self._dump_jsonl(
            derived / 'chunks.jsonl',
            select(Chunk).where(Chunk.vault_id == vid),
            'chunks',
            self._chunk_to_export,
        )
        await self._dump_jsonl(
            derived / 'nodes.jsonl',
            select(Node).where(Node.vault_id == vid),
            'nodes',
            self._node_to_export,
        )
        await self._dump_jsonl(
            derived / 'memory_units.jsonl',
            select(MemoryUnit).where(MemoryUnit.vault_id == vid),
            'memory_units',
            self._unit_to_export,
        )
        await self._dump_jsonl(
            derived / 'unit_entities.jsonl',
            select(UnitEntity).where(UnitEntity.vault_id == vid),
            'unit_entities',
            self._unit_entity_to_export,
        )
        await self._dump_jsonl(
            derived / 'memory_links.jsonl',
            select(MemoryLink).where(MemoryLink.vault_id == vid),
            'memory_links',
            self._link_to_export,
        )
        await self._dump_jsonl(
            derived / 'mental_models.jsonl',
            select(MentalModel).where(MentalModel.vault_id == vid),
            'mental_models',
            self._mental_to_export,
        )
        await self._dump_jsonl(
            derived / 'entity_cooccurrences.jsonl',
            select(EntityCooccurrence).where(EntityCooccurrence.vault_id == vid),
            'entity_cooccurrences',
            self._cooccur_to_export,
        )

        # Entity reference UNION (defended in V12 plan Decision 1):
        # union of entity_ids referenced by unit_entities, memory_links,
        # mental_models, entity_cooccurrences. Then we export Entity +
        # EntityAlias rows for that union (global tables — no vault_id).
        entity_ids = await self._compute_entity_reference_set(vid)
        await self._write_entities(derived, entity_ids)
        await self._write_entity_aliases(derived, entity_ids)

        # Vault summary (1 row per vault) — written as a single JSON file.
        await self._write_vault_summary(derived)

        # NoteAppend has no vault_id; filter transitively via note_id.
        await self._write_note_appends(derived, vid)

    async def _compute_entity_reference_set(self, vault_id: UUID) -> set[UUID]:
        """UNION of entity_id columns across all vault-scoped derived tables."""
        ids: set[UUID] = set()

        for stmt in (
            select(UnitEntity.entity_id).where(UnitEntity.vault_id == vault_id),
            select(MemoryLink.entity_id)
            .where(MemoryLink.vault_id == vault_id)
            .where(MemoryLink.entity_id.is_not(None)),  # type: ignore[union-attr]
            select(MentalModel.entity_id).where(MentalModel.vault_id == vault_id),
            select(EntityCooccurrence.entity_id_1).where(EntityCooccurrence.vault_id == vault_id),
            select(EntityCooccurrence.entity_id_2).where(EntityCooccurrence.vault_id == vault_id),
        ):
            result = await self._session.execute(stmt)
            for (eid,) in result.all():
                if eid is not None:
                    ids.add(eid)
        return ids

    async def _write_entities(self, derived: Path, ids: set[UUID]) -> None:
        path = derived / 'entities.jsonl'
        if not ids:
            path.write_text('', encoding='utf-8')
            self._table_counts['entities'] = 0
            return
        result = await self._session.execute(select(Entity).where(Entity.id.in_(ids)))  # type: ignore[attr-defined]
        rows = list(result.scalars())
        with path.open('w', encoding='utf-8') as fp:
            for row in rows:
                export = EntityExport(
                    id=row.id,
                    canonical_name=row.canonical_name,
                    phonetic_code=row.phonetic_code,
                    entity_type=row.entity_type,
                    first_seen=row.first_seen,
                    last_seen=row.last_seen,
                    mention_count=row.mention_count,
                    retrieval_count=row.retrieval_count,
                    last_retrieved_at=row.last_retrieved_at,
                )
                fp.write(_dump_jsonl_line(export) + '\n')
        self._table_counts['entities'] = len(rows)

    async def _write_entity_aliases(self, derived: Path, ids: set[UUID]) -> None:
        path = derived / 'entity_aliases.jsonl'
        if not ids:
            path.write_text('', encoding='utf-8')
            self._table_counts['entity_aliases'] = 0
            return
        result = await self._session.execute(
            select(EntityAlias).where(EntityAlias.canonical_id.in_(ids))  # type: ignore[attr-defined]
        )
        rows = list(result.scalars())
        with path.open('w', encoding='utf-8') as fp:
            for row in rows:
                export = EntityAliasExport(
                    id=row.id,
                    canonical_id=row.canonical_id,
                    name=row.name,
                    phonetic_code=row.phonetic_code,
                )
                fp.write(_dump_jsonl_line(export) + '\n')
        self._table_counts['entity_aliases'] = len(rows)

    async def _write_vault_summary(self, derived: Path) -> None:
        assert self._vault is not None
        result = await self._session.execute(
            select(VaultSummary).where(VaultSummary.vault_id == self._vault.id)
        )
        row = result.scalar_one_or_none()
        path = derived / 'vault_summary.json'
        if row is None:
            # Absent file = no summary. Don't write an empty stub.
            self._table_counts['vault_summaries'] = 0
            return
        export = VaultSummaryExport(
            id=row.id,
            vault_id=row.vault_id,
            narrative=row.narrative,
            themes=row.themes or [],
            inventory=row.inventory or {},
            key_entities=row.key_entities or [],
            version=row.version,
            notes_incorporated=row.notes_incorporated,
            patch_log=row.patch_log or [],
            needs_regeneration=row.needs_regeneration,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        self._write_json(path, _to_jsonable(export))
        self._table_counts['vault_summaries'] = 1

    async def _write_note_appends(self, derived: Path, vault_id: UUID) -> None:
        path = derived / 'note_appends.jsonl'
        # Transitive filter: NoteAppend has no vault_id; filter via note_id.
        stmt = select(NoteAppend).where(
            NoteAppend.note_id.in_(  # type: ignore[attr-defined]
                select(Note.id).where(Note.vault_id == vault_id)
            )
        )
        result = await self._session.execute(stmt)
        rows = list(result.scalars())
        with path.open('w', encoding='utf-8') as fp:
            for row in rows:
                export = NoteAppendExport(
                    append_id=row.append_id,
                    note_id=row.note_id,
                    delta_sha256=row.delta_sha256,
                    delta_bytes=row.delta_bytes,
                    joiner=row.joiner,
                    resulting_content_hash=row.resulting_content_hash,
                    new_unit_ids=list(row.new_unit_ids or []),
                    applied_at=row.applied_at,
                )
                fp.write(_dump_jsonl_line(export) + '\n')
        self._table_counts['note_appends'] = len(rows)

    # ------------------------------------------------------------------
    # Governance (JSONL)

    async def _write_governance(self) -> None:
        assert self._vault is not None
        gov = self._output_dir / 'governance'
        gov.mkdir(exist_ok=True)
        vid = self._vault.id

        # MaintenanceProposal.vault_id is nullable (NULL = global findings,
        # reserved for Tier B). Per Decision 13, per-vault export
        # excludes NULLs.
        await self._dump_jsonl(
            gov / 'maintenance_proposals.jsonl',
            select(MaintenanceProposal).where(MaintenanceProposal.vault_id == vid),
            'maintenance_proposals',
            self._proposal_to_export,
        )
        await self._dump_jsonl(
            gov / 'procedure_outcomes.jsonl',
            select(ProcedureOutcome).where(ProcedureOutcome.vault_id == vid),
            'procedure_outcomes',
            self._outcome_to_export,
        )

    # ------------------------------------------------------------------
    # Per-row converters (kept tiny — Pydantic does the heavy lifting)

    @staticmethod
    def _chunk_to_export(row: Chunk) -> ChunkExport:
        return ChunkExport(
            id=row.id,
            vault_id=row.vault_id,
            note_id=row.note_id,
            text=row.text,
            content_hash=row.content_hash,
            status=coerce_enum_value(row.status),
            chunk_index=row.chunk_index,
            summary=row.summary,
            summary_formatted=row.summary_formatted,
            created_at=row.created_at,
        )

    @staticmethod
    def _node_to_export(row: Node) -> NodeExport:
        return NodeExport(
            id=row.id,
            vault_id=row.vault_id,
            note_id=row.note_id,
            block_id=row.block_id,
            node_hash=row.node_hash,
            title=row.title,
            text=row.text,
            summary=row.summary,
            summary_formatted=row.summary_formatted,
            level=row.level,
            seq=row.seq,
            token_estimate=row.token_estimate,
            status=coerce_enum_value(row.status),
            created_at=row.created_at,
        )

    @staticmethod
    def _unit_to_export(row: MemoryUnit) -> MemoryUnitExport:
        return MemoryUnitExport(
            id=row.id,
            vault_id=row.vault_id,
            note_id=row.note_id,
            chunk_id=row.chunk_id,
            text=row.text,
            fact_type=coerce_enum_value(row.fact_type),
            status=coerce_enum_value(row.status),
            event_date=row.event_date,
            occurred_start=row.occurred_start,
            occurred_end=row.occurred_end,
            mentioned_at=row.mentioned_at,
            context=row.context,
            is_deprioritized=row.is_deprioritized,
            success_co_count=row.success_co_count,
            failure_co_count=row.failure_co_count,
            intent_class=coerce_enum_value(row.intent_class),
            risk_class=coerce_enum_value(row.risk_class),
            confidence=row.confidence,
            confidence_evidence_count=row.confidence_evidence_count,
            importance=row.importance,
            stability=row.stability,
            last_outcome_at=row.last_outcome_at,
            unit_metadata=row.unit_metadata or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _unit_entity_to_export(row: UnitEntity) -> UnitEntityExport:
        return UnitEntityExport(
            unit_id=row.unit_id,
            entity_id=row.entity_id,
            vault_id=row.vault_id,
            success_co_count=row.success_co_count,
            failure_co_count=row.failure_co_count,
        )

    @staticmethod
    def _link_to_export(row: MemoryLink) -> MemoryLinkExport:
        return MemoryLinkExport(
            from_unit_id=row.from_unit_id,
            to_unit_id=row.to_unit_id,
            link_type=coerce_enum_value(row.link_type),
            vault_id=row.vault_id,
            entity_id=row.entity_id,
            link_metadata=row.link_metadata or {},
            weight=row.weight,
            created_at=row.created_at,
        )

    @staticmethod
    def _mental_to_export(row: MentalModel) -> MentalModelExport:
        return MentalModelExport(
            id=row.id,
            vault_id=row.vault_id,
            entity_id=row.entity_id,
            name=row.name,
            observations=list(row.observations or []),
            entity_metadata=row.entity_metadata or {},
            last_refreshed=row.last_refreshed,
            version=row.version,
            success_co_count=row.success_co_count,
            failure_co_count=row.failure_co_count,
        )

    @staticmethod
    def _cooccur_to_export(row: EntityCooccurrence) -> EntityCooccurrenceExport:
        return EntityCooccurrenceExport(
            entity_id_1=row.entity_id_1,
            entity_id_2=row.entity_id_2,
            vault_id=row.vault_id,
            cooccurrence_count=row.cooccurrence_count,
            last_cooccurred=row.last_cooccurred,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
        )

    @staticmethod
    def _proposal_to_export(row: MaintenanceProposal) -> MaintenanceProposalExport:
        # vault_id is nullable in the schema; the SQL filter excludes NULL,
        # but assert defensively here.
        if row.vault_id is None:
            raise SnapshotExportError(
                f'MaintenanceProposal {row.id} has NULL vault_id but reached the export step.'
            )
        return MaintenanceProposalExport(
            id=row.id,
            vault_id=row.vault_id,
            lint_type=coerce_enum_value(row.lint_type),
            target_type=row.target_type,
            target_id=row.target_id,
            rule_name=row.rule_name,
            evidence=row.evidence or {},
            suggested_action=row.suggested_action,
            status=coerce_enum_value(row.status),
            source=coerce_enum_value(row.source),
            created_at=row.created_at,
            resolved_at=row.resolved_at,
            resolved_by=row.resolved_by,
        )

    @staticmethod
    def _outcome_to_export(row: ProcedureOutcome) -> ProcedureOutcomeExport:
        return ProcedureOutcomeExport(
            id=row.id,
            vault_id=row.vault_id,
            kv_key=row.kv_key,
            success_co_count=row.success_co_count,
            failure_co_count=row.failure_co_count,
            last_outcome_at=row.last_outcome_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # Generic JSONL writer

    async def _dump_jsonl(
        self,
        path: Path,
        statement: Any,
        count_key: str,
        converter: Any,
    ) -> None:
        result = await self._session.execute(statement)
        rows = list(result.scalars())
        with path.open('w', encoding='utf-8') as fp:
            for row in rows:
                export = converter(row)
                fp.write(_dump_jsonl_line(export) + '\n')
        self._table_counts[count_key] = len(rows)

    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, allow_nan=False, sort_keys=True, ensure_ascii=False),
            encoding='utf-8',
        )

    def _write_readme(self, manifest: SnapshotManifest) -> None:
        content = (
            '# Memex vault snapshot\n\n'
            f'Snapshot version: {manifest.snapshot_version}\n'
            f'Exported at: {manifest.exported_at.isoformat()}\n\n'
            'See ``manifest.json`` for the machine-readable header. JSONL '
            'files under ``derived/`` are line-delimited JSON; one record '
            'per line. ``notes/<dir>/note.md`` holds the original note '
            'text. ``notes/<dir>/metadata.json`` carries note metadata '
            'with asset paths rewritten to relative form.\n\n'
            'This snapshot is consumed by the eval-only import (V12) '
            'inside the memex_eval package. Other downstream consumers '
            'pin to the snapshot SemVer in ``manifest.json::snapshot_version`` '
            'and parse the JSONL schemas accordingly.\n'
        )
        (self._output_dir / 'README.md').write_text(content, encoding='utf-8')
