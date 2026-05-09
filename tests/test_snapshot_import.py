"""Integration tests for V12 eval-only snapshot import.

These exercise the round-trip: build a vault, export with V3, import with
V12 into a fresh target vault on the same DB, and assert table counts +
load-bearing column preservation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, OnnxBackend
from memex_core.memory.sql_models import (
    Chunk,
    Entity,
    EntityAlias,
    EntityCooccurrence,
    MaintenanceProposal,
    MemoryLink,
    MemoryUnit,
    MentalModel,
    Note,
    NoteAppend,
    UnitEntity,
    Vault,
    VaultSummary,
)
from memex_core.services.snapshot import (
    OBSERVATION_SCHEMA_VERSION,
    SNAPSHOT_VERSION,
    SnapshotExporter,
    SnapshotImporter,
    SnapshotImportRefused,
    ensure_eval_import_state_table,
    validate_snapshot_dir,
)
from memex_core.services.snapshot.import_models import (
    ObservationV1,
)
from memex_core.services.snapshot.manifest import (
    EmbeddingModelIdentity,
    SnapshotManifest,
    SnapshotVersion,
)
from memex_core.services.snapshot.path_validation import SnapshotPathError


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ----------------------------------------------------------------------
# Fixtures


@pytest_asyncio.fixture
async def populated_vault(db_session: AsyncSession) -> dict[str, UUID]:
    import hashlib

    vault = Vault(name='import-test', description='V12 round-trip fixture')
    db_session.add(vault)
    await db_session.flush()

    note_text = 'Body text used for V12 round-trip.'
    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        title='Round-trip kickoff',
        description='import test note',
        original_text=note_text,
        content_hash=hashlib.md5(note_text.encode('utf-8')).hexdigest(),
        status='active',
        doc_metadata={'source': 'fixture'},
    )
    db_session.add(note)
    await db_session.flush()

    chunk = Chunk(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='Round-trip kickoff body.',
        content_hash='hash-chunk-1',
        chunk_index=0,
        embedding=[0.1] * 384,
    )
    db_session.add(chunk)
    await db_session.flush()

    # Specific MW counters + status combinations to catch silent zeroing.
    unit_a = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        chunk_id=chunk.id,
        text='Sarah Chen leads the round-trip project.',
        fact_type='world',
        status='active',
        intent_class='durable',
        risk_class='none',
        confidence=0.93,
        confidence_evidence_count=2,
        event_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        is_deprioritized=True,
        success_co_count=7,
        failure_co_count=3,
        embedding=[0.2] * 384,
        importance=0.6,
        stability=0.4,
    )
    unit_b = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        chunk_id=chunk.id,
        text='Project Alpha is internal.',
        fact_type='world',
        status='active',
        intent_class='ephemeral',
        risk_class='private',
        confidence=0.42,
        confidence_evidence_count=0,
        event_date=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        is_deprioritized=False,
        success_co_count=0,
        failure_co_count=0,
        embedding=[0.3] * 384,
    )
    db_session.add(unit_a)
    db_session.add(unit_b)
    await db_session.flush()

    entity_a = Entity(
        id=uuid4(),
        canonical_name='Sarah Chen',
        entity_type='Person',
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 5, 1, tzinfo=timezone.utc),
        mention_count=5,
        retrieval_count=2,
    )
    entity_b = Entity(
        id=uuid4(),
        canonical_name='Project Alpha',
        entity_type='Concept',
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 5, 2, tzinfo=timezone.utc),
        mention_count=3,
        retrieval_count=1,
    )
    db_session.add(entity_a)
    db_session.add(entity_b)
    await db_session.flush()

    alias = EntityAlias(id=uuid4(), canonical_id=entity_a.id, name='S. Chen', phonetic_code='SRXN')
    db_session.add(alias)

    db_session.add(UnitEntity(unit_id=unit_a.id, entity_id=entity_a.id, vault_id=vault.id))
    db_session.add(UnitEntity(unit_id=unit_a.id, entity_id=entity_b.id, vault_id=vault.id))

    eid1, eid2 = sorted([entity_a.id, entity_b.id])
    db_session.add(
        EntityCooccurrence(
            entity_id_1=eid1,
            entity_id_2=eid2,
            vault_id=vault.id,
            cooccurrence_count=4,
            last_cooccurred=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
    )

    # MemoryLink uses composite PK (from_unit_id, to_unit_id, link_type).
    db_session.add(
        MemoryLink(
            from_unit_id=unit_a.id,
            to_unit_id=unit_b.id,
            link_type='semantic',
            vault_id=vault.id,
            entity_id=entity_b.id,
            link_metadata={'reason': 'fixture'},
            weight=0.7,
        )
    )

    db_session.add(
        MentalModel(
            id=uuid4(),
            vault_id=vault.id,
            entity_id=entity_a.id,
            name='Sarah Chen',
            observations=[
                {
                    'id': str(uuid4()),
                    'title': 'Leads project',
                    'content': 'Sarah leads the round-trip project.',
                    'trend': 'stable',
                    'evidence': [],
                }
            ],
            entity_metadata={'kind': 'person'},
            last_refreshed=datetime(2026, 5, 1, tzinfo=timezone.utc),
            version=1,
            success_co_count=2,
            failure_co_count=1,
        )
    )

    db_session.add(
        VaultSummary(
            id=uuid4(),
            vault_id=vault.id,
            narrative='Round-trip narrative.',
            themes=[{'name': 'roundtrip'}],
            inventory={'notes': 1},
            key_entities=[{'name': 'Sarah Chen'}],
            version=1,
            notes_incorporated=1,
            patch_log=[],
            needs_regeneration=False,
        )
    )

    db_session.add(
        MaintenanceProposal(
            id=uuid4(),
            vault_id=vault.id,
            lint_type='structural',
            target_type='note',
            target_id=str(note.id),
            rule_name='roundtrip-fixture',
            evidence={'hint': 'fixture'},
            suggested_action='archive',
            status='pending',
            source='rule',
        )
    )

    await db_session.commit()
    return {
        'vault_id': vault.id,
        'note_id': note.id,
        'chunk_id': chunk.id,
        'unit_a_id': unit_a.id,
        'unit_b_id': unit_b.id,
        'entity_a_id': entity_a.id,
        'entity_b_id': entity_b.id,
    }


@pytest_asyncio.fixture
async def eval_state_table(db_session: AsyncSession) -> None:
    """Apply eval-mode-only DDL once per test."""
    conn = await db_session.connection()
    await ensure_eval_import_state_table(conn)
    await db_session.commit()


# ----------------------------------------------------------------------
# Unit tests — pure validation


class TestVersionGate:
    def test_pinned_major_minor_constants(self) -> None:
        from memex_core.services.snapshot.import_models import (
            PINNED_SNAPSHOT_MAJOR,
            PINNED_SNAPSHOT_MINOR,
        )

        assert PINNED_SNAPSHOT_MAJOR == 1
        assert PINNED_SNAPSHOT_MINOR == 1

    def test_snapshot_version_parsing(self) -> None:
        v = SnapshotVersion.parse('1.1.0')
        assert v.major == 1
        assert v.minor == 1
        assert v.patch == 0

        with pytest.raises(ValueError):
            SnapshotVersion.parse('1.1')

    def test_higher_minor_accepted_via_extra_ignore(self) -> None:
        # Forward-compat: import models must ignore extra fields.
        from memex_core.services.snapshot.import_models import VaultImport

        raw = {
            'id': str(uuid4()),
            'name': 'test',
            'description': None,
            'mw_mode': 'standard',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'future_field': 'ignored',  # MINOR-bumped field
        }
        # Should NOT raise.
        v = VaultImport.model_validate(raw)
        assert v.name == 'test'
        assert not hasattr(v, 'future_field')


class TestPathValidation:
    def test_rejects_path_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / 'allowlist'
        root.mkdir()
        outside = tmp_path / 'evil-snapshot'
        outside.mkdir()
        with pytest.raises(SnapshotPathError):
            validate_snapshot_dir(outside, allowlist_root=root)

    def test_accepts_path_inside_root(self, tmp_path: Path) -> None:
        root = tmp_path / 'allowlist'
        root.mkdir()
        inside = root / 'snap'
        inside.mkdir()
        resolved = validate_snapshot_dir(inside, allowlist_root=root)
        assert resolved == inside.resolve()

    def test_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        root = tmp_path / 'allowlist'
        root.mkdir()
        with pytest.raises(SnapshotPathError):
            validate_snapshot_dir(root / 'does-not-exist', allowlist_root=root)


class TestPathValidationTOCTOU:
    """O_NOFOLLOW + post-open realpath defense (Decision 9)."""

    def test_open_validated_rejects_path_outside_root(self, tmp_path: Path) -> None:
        """O_NOFOLLOW + /proc/self/fd realpath catches a file outside the root."""
        from memex_core.services.snapshot.path_validation import open_validated

        root = tmp_path / 'allowlist'
        root.mkdir()
        outside = tmp_path / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        # Caller passes the absolute path of `outside`. Even if a clever
        # attacker tricked validate_snapshot_dir, open_validated's
        # post-open realpath check refuses.
        with pytest.raises(SnapshotPathError, match='escaped allowlist'):
            open_validated(outside.resolve(), expected_root=root)

    def test_open_validated_refuses_symlink_at_leaf(self, tmp_path: Path) -> None:
        """O_NOFOLLOW on the leaf path makes os.open raise ELOOP."""
        from memex_core.services.snapshot.path_validation import open_validated

        root = tmp_path / 'allowlist'
        root.mkdir()
        target = root / 'real.txt'
        target.write_text('hi', encoding='utf-8')
        link = root / 'leaf-link.txt'
        link.symlink_to(target)
        with pytest.raises(OSError):
            # O_NOFOLLOW refuses to follow a leaf symlink → ELOOP.
            open_validated(link, expected_root=root)

    def test_open_validated_accepts_real_file(self, tmp_path: Path) -> None:
        from memex_core.services.snapshot.path_validation import (
            open_validated,
            read_validated_text,
        )

        root = tmp_path / 'allowlist'
        root.mkdir()
        f = root / 'real.txt'
        f.write_text('hello', encoding='utf-8')
        fd = open_validated(f.resolve(), expected_root=root)
        # open_validated returns a fd; close it.
        import os as _os

        _os.close(fd)
        # read_validated_text round-trip
        assert read_validated_text(f.resolve(), expected_root=root) == 'hello'


class TestObservationV1:
    def test_extra_field_refused(self) -> None:
        with pytest.raises(ValidationError):
            ObservationV1.model_validate(
                {
                    'id': str(uuid4()),
                    'title': 't',
                    'content': 'c',
                    'trend': 'new',
                    'evidence': [],
                    'mystery_field': 'no',
                }
            )

    def test_minimal_valid(self) -> None:
        obs = ObservationV1.model_validate(
            {
                'id': str(uuid4()),
                'title': 't',
                'content': 'c',
                'trend': 'new',
            }
        )
        assert obs.evidence == []


# ----------------------------------------------------------------------
# Integration: round-trip


def _build_manifest_for(vault_id: UUID, alembic_head: str) -> SnapshotManifest:
    from memex_core.memory.models.base import MODEL_REGISTRY
    from memex_core.memory.sql_models import EMBEDDING_DIMENSION

    spec = MODEL_REGISTRY['embedding']
    return SnapshotManifest(
        snapshot_version=SNAPSHOT_VERSION,
        source_vault_id=vault_id,
        source_vault_name='import-test',
        exported_at=datetime.now(timezone.utc),
        alembic_head=alembic_head,
        embedding_model=EmbeddingModelIdentity(
            name=str(spec.repo_id), dim=EMBEDDING_DIMENSION, hash=str(spec.revision)
        ),
        observation_schema_version=OBSERVATION_SCHEMA_VERSION,
        table_counts={},
    )


async def _export(
    db_session: AsyncSession,
    vault_id: UUID,
    output_dir: Path,
) -> Path:
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=vault_id,
        output_dir=output_dir,
    )
    await exporter.export()
    # Exporter leaves a REPEATABLE READ READ ONLY transaction open. Roll
    # back to release that isolation before the importer wants to INSERT.
    await db_session.rollback()
    return output_dir


async def _capture_counts(db_session: AsyncSession, vault_id: UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model, label in [
        (Note, 'notes'),
        (Chunk, 'chunks'),
        (MemoryUnit, 'units'),
        (UnitEntity, 'unit_entities'),
        (EntityCooccurrence, 'cooccurrences'),
        (MentalModel, 'mental_models'),
        (MemoryLink, 'links'),
        (VaultSummary, 'vault_summaries'),
        (MaintenanceProposal, 'proposals'),
    ]:
        c = (
            await db_session.execute(
                select(func.count()).select_from(model).where(model.vault_id == vault_id)
            )
        ).scalar_one()
        counts[label] = c
    return counts


async def _delete_source_vault(db_session: AsyncSession, vault_id: UUID) -> None:
    """Drop the source vault so its UUIDs free up for re-import.

    Eval workflow imports into an empty DB; tests reuse the same DB across
    export+import phases, so we have to delete the source manually to avoid
    PK collisions on Note.id, MemoryUnit.id, etc. (Decision 2.)
    """
    from sqlalchemy import delete

    # Order matters: child tables first.
    await db_session.execute(delete(MemoryLink).where(MemoryLink.vault_id == vault_id))
    await db_session.execute(delete(UnitEntity).where(UnitEntity.vault_id == vault_id))
    await db_session.execute(
        delete(EntityCooccurrence).where(EntityCooccurrence.vault_id == vault_id)
    )
    await db_session.execute(delete(MentalModel).where(MentalModel.vault_id == vault_id))
    await db_session.execute(delete(MemoryUnit).where(MemoryUnit.vault_id == vault_id))
    await db_session.execute(delete(Chunk).where(Chunk.vault_id == vault_id))
    await db_session.execute(delete(VaultSummary).where(VaultSummary.vault_id == vault_id))
    await db_session.execute(
        delete(MaintenanceProposal).where(MaintenanceProposal.vault_id == vault_id)
    )
    await db_session.execute(delete(Note).where(Note.vault_id == vault_id))
    await db_session.execute(delete(Vault).where(Vault.id == vault_id))
    await db_session.commit()


async def test_round_trip_row_counts(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    # Export first (must run in a fresh transaction so SET ISOLATION LEVEL
    # works), then count, then delete the source so the importer can reuse
    # the original UUIDs.
    await _export(db_session, src_vault, snapshot_dir)
    src_counts = await _capture_counts(db_session, src_vault)
    await _delete_source_vault(db_session, src_vault)

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='roundtrip-target',
    )
    target_vault_id = await importer.import_snapshot()
    assert target_vault_id != src_vault

    dst_counts = await _capture_counts(db_session, target_vault_id)
    for label in src_counts:
        assert src_counts[label] == dst_counts[label], (
            f'{label}: src={src_counts[label]} dst={dst_counts[label]}'
        )


async def test_round_trip_preserves_mw_counters_and_timestamps(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)

    src_unit = (
        await db_session.execute(
            select(MemoryUnit).where(
                MemoryUnit.id == populated_vault['unit_a_id'],
                MemoryUnit.vault_id == src_vault,
            )
        )
    ).scalar_one()
    src_snapshot = {
        'is_deprioritized': src_unit.is_deprioritized,
        'success_co_count': src_unit.success_co_count,
        'failure_co_count': src_unit.failure_co_count,
        'confidence': src_unit.confidence,
        'confidence_evidence_count': src_unit.confidence_evidence_count,
        'created_at': src_unit.created_at,
        'updated_at': src_unit.updated_at,
    }
    await _delete_source_vault(db_session, src_vault)

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='roundtrip-mw',
    )
    target_vault_id = await importer.import_snapshot()

    dst_unit = (
        await db_session.execute(
            select(MemoryUnit).where(
                MemoryUnit.id == populated_vault['unit_a_id'],
                MemoryUnit.vault_id == target_vault_id,
            )
        )
    ).scalar_one()

    # Load-bearing for FSFM scoring — must round-trip exactly.
    assert dst_unit.is_deprioritized is True
    assert dst_unit.success_co_count == src_snapshot['success_co_count'] == 7
    assert dst_unit.failure_co_count == src_snapshot['failure_co_count'] == 3
    assert dst_unit.confidence == src_snapshot['confidence']
    assert dst_unit.confidence_evidence_count == src_snapshot['confidence_evidence_count']
    assert dst_unit.created_at == src_snapshot['created_at']
    assert dst_unit.updated_at == src_snapshot['updated_at']


async def test_round_trip_uuid_preserved_for_intra_snapshot_fks(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Note/Chunk/MemoryUnit IDs preserved verbatim; only vault_id is rewritten."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='roundtrip-fk',
    )
    target_vault_id = await importer.import_snapshot()

    # Note id preserved.
    note = (
        await db_session.execute(
            select(Note).where(
                Note.vault_id == target_vault_id, Note.id == populated_vault['note_id']
            )
        )
    ).scalar_one()
    assert note.vault_id == target_vault_id

    # MemoryLink intra-snapshot FK still resolves.
    link = (
        await db_session.execute(select(MemoryLink).where(MemoryLink.vault_id == target_vault_id))
    ).scalar_one()
    assert link.from_unit_id == populated_vault['unit_a_id']
    assert link.to_unit_id == populated_vault['unit_b_id']


async def test_round_trip_embeddings_are_filled(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Phase D regenerates embeddings via the local model."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='roundtrip-embed',
    )
    target_vault_id = await importer.import_snapshot()

    null_chunks = (
        await db_session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.vault_id == target_vault_id, Chunk.embedding.is_(None))  # type: ignore[union-attr]
        )
    ).scalar_one()
    assert null_chunks == 0

    null_units = (
        await db_session.execute(
            select(func.count())
            .select_from(MemoryUnit)
            .where(
                MemoryUnit.vault_id == target_vault_id,
                MemoryUnit.embedding.is_(None),  # type: ignore[union-attr]
            )
        )
    ).scalar_one()
    assert null_units == 0


async def test_import_refuses_global_vault(
    db_session: AsyncSession,
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    from sqlalchemy import text as sa_text

    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    # Hand-craft a snapshot dir with vault.json declaring the global vault.
    from memex_core.services.snapshot.export_models import VaultExport

    db_head = (
        await db_session.execute(sa_text('SELECT version_num FROM alembic_version'))
    ).scalar_one()
    manifest = _build_manifest_for(GLOBAL_VAULT_ID, db_head)
    (snapshot_dir / 'manifest.json').write_text(manifest.model_dump_json())
    (snapshot_dir / 'vault.json').write_text(
        VaultExport(
            id=GLOBAL_VAULT_ID,
            name='global',
            mw_mode='standard',
            created_at=datetime.now(timezone.utc),
        ).model_dump_json()
    )

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='global-refuse',
    )
    with pytest.raises(SnapshotImportRefused, match='global vault'):
        await importer.import_snapshot()


async def test_import_refuses_alembic_head_mismatch(
    db_session: AsyncSession,
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()

    from memex_core.services.snapshot.export_models import VaultExport

    src_vault_id = uuid4()
    manifest = _build_manifest_for(src_vault_id, 'WRONG-HEAD')
    (snapshot_dir / 'manifest.json').write_text(manifest.model_dump_json())
    (snapshot_dir / 'vault.json').write_text(
        VaultExport(
            id=src_vault_id,
            name='import-test',
            mw_mode='standard',
            created_at=datetime.now(timezone.utc),
        ).model_dump_json()
    )

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='alembic-refuse',
    )
    with pytest.raises(SnapshotImportRefused, match='Alembic head mismatch'):
        await importer.import_snapshot()


async def test_import_refuses_major_version_mismatch(
    db_session: AsyncSession,
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()

    from memex_core.memory.models.base import MODEL_REGISTRY
    from memex_core.memory.sql_models import EMBEDDING_DIMENSION
    from memex_core.services.snapshot.export_models import VaultExport

    spec = MODEL_REGISTRY['embedding']
    src_vault_id = uuid4()
    manifest_data = {
        'snapshot_version': '2.0.0',
        'source_vault_id': str(src_vault_id),
        'source_vault_name': 'import-test',
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'alembic_head': 'fake',
        'embedding_model': {
            'name': str(spec.repo_id),
            'dim': EMBEDDING_DIMENSION,
            'hash': str(spec.revision),
        },
        'observation_schema_version': OBSERVATION_SCHEMA_VERSION,
        'table_counts': {},
    }
    (snapshot_dir / 'manifest.json').write_text(json.dumps(manifest_data))
    (snapshot_dir / 'vault.json').write_text(
        VaultExport(
            id=src_vault_id,
            name='import-test',
            mw_mode='standard',
            created_at=datetime.now(timezone.utc),
        ).model_dump_json()
    )

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='major-refuse',
    )
    with pytest.raises(SnapshotImportRefused, match='Snapshot MAJOR'):
        await importer.import_snapshot()


async def test_import_refuses_remote_embedding_backend(
    db_session: AsyncSession,
    tmp_path: Path,
    populated_vault: dict[str, UUID],
    eval_state_table: None,
) -> None:
    from memex_common.config import LitellmEmbeddingBackend

    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await db_session.rollback()

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=LitellmEmbeddingBackend(model='openai/text-embedding-3-small'),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='remote-refuse',
    )
    with pytest.raises(SnapshotImportRefused, match='Remote'):
        await importer.import_snapshot()


async def test_import_records_state_complete(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    from sqlalchemy import text

    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='state-complete',
    )
    target_vault_id = await importer.import_snapshot()

    result = await db_session.execute(
        text('SELECT state FROM eval_import_state WHERE target_vault_id = :v'),
        {'v': str(target_vault_id)},
    )
    assert result.scalar_one() == 'complete'


async def test_import_second_attempt_refused(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Second import using the SAME target_vault_id is refused (Decision 20).

    Fresh allocations don't collide in production; this tests the defensive
    refusal directly.
    """
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    importer1 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='dup-1',
    )
    target_id = await importer1.import_snapshot()

    importer2 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='dup-2',
    )
    importer2._target_vault_id = target_id  # force collision
    with pytest.raises(SnapshotImportRefused, match='already imported'):
        await importer2.import_snapshot()


async def test_higher_minor_manifest_accepted(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """A v1.2 manifest with a future field still imports on a v1.1 importer."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    # Inject a future-MINOR manifest on top of the freshly-exported one.
    manifest_path = snapshot_dir / 'manifest.json'
    raw = json.loads(manifest_path.read_text())
    raw['snapshot_version'] = '1.2.0'
    raw['some_v1_2_field'] = 'should-be-ignored'
    raw['embedding_model']['some_future_attr'] = 'also-ignored'
    manifest_path.write_text(json.dumps(raw))

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='minor-bump',
    )
    target_vault_id = await importer.import_snapshot()
    assert target_vault_id is not None


async def test_import_resumes_partial(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Decision 10 idempotency: a partial import resumes by source path."""
    from sqlalchemy import text as sa_text

    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    # First attempt — succeed all the way through.
    importer1 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='resume-target',
    )
    target_vault_id_1 = await importer1.import_snapshot()

    # Manually walk the state back to 'embedded' to simulate a crash AFTER
    # Phase D succeeded but BEFORE Phase E flipped to 'complete'.
    await db_session.execute(
        sa_text("UPDATE eval_import_state SET state='embedded' WHERE target_vault_id = :v"),
        {'v': str(target_vault_id_1)},
    )
    await db_session.commit()

    # Second attempt — must adopt the existing target_vault_id and import_id,
    # not allocate new ones (which would PK-collide on Note.id etc.).
    importer2 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='resume-target',
    )
    target_vault_id_2 = await importer2.import_snapshot()
    assert target_vault_id_2 == target_vault_id_1
    # Single eval_import_state row.
    count = (
        await db_session.execute(
            sa_text('SELECT COUNT(*) FROM eval_import_state WHERE source_snapshot_path = :p'),
            {'p': str(snapshot_dir.resolve())},
        )
    ).scalar_one()
    assert count == 1


async def test_import_refuses_completed_resubmit(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Resubmitting a completed import for the same source path refuses."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    importer1 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='complete-1',
    )
    await importer1.import_snapshot()

    importer2 = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='complete-2',
    )
    with pytest.raises(SnapshotImportRefused, match='already imported'):
        await importer2.import_snapshot()


async def test_import_refuses_existing_vault_name(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Pre-flight refuses on Vault.name UNIQUE collision (no Phase B ugly error)."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    # Don't delete source — its vault stays around with name 'import-test'.

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='import-test',  # collides
    )
    with pytest.raises(SnapshotImportRefused, match='already exists'):
        await importer.import_snapshot()


async def test_import_refuses_entity_canonical_name_collision(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Pre-check refuses when an Entity.canonical_name in the snapshot
    already exists in the importing DB with a different id."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)

    # Delete the source vault rows but KEEP its entities (global table).
    # Then drop+re-add an Entity with the same canonical_name but a new id,
    # so the imported snapshot tries to create a duplicate-name with a
    # different id.
    from sqlalchemy import delete as sa_delete

    src_entity_a = populated_vault['entity_a_id']
    await db_session.execute(sa_delete(MemoryLink).where(MemoryLink.vault_id == src_vault))
    await db_session.execute(sa_delete(UnitEntity).where(UnitEntity.vault_id == src_vault))
    await db_session.execute(
        sa_delete(EntityCooccurrence).where(EntityCooccurrence.vault_id == src_vault)
    )
    await db_session.execute(sa_delete(MentalModel).where(MentalModel.vault_id == src_vault))
    await db_session.execute(sa_delete(MemoryUnit).where(MemoryUnit.vault_id == src_vault))
    await db_session.execute(sa_delete(Chunk).where(Chunk.vault_id == src_vault))
    await db_session.execute(sa_delete(VaultSummary).where(VaultSummary.vault_id == src_vault))
    await db_session.execute(
        sa_delete(MaintenanceProposal).where(MaintenanceProposal.vault_id == src_vault)
    )
    await db_session.execute(sa_delete(Note).where(Note.vault_id == src_vault))
    await db_session.execute(sa_delete(Vault).where(Vault.id == src_vault))
    await db_session.execute(sa_delete(Entity).where(Entity.id == src_entity_a))
    db_session.add(
        Entity(
            id=uuid4(),
            canonical_name='Sarah Chen',  # same name, different id
            entity_type='Person',
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='entity-collision',
    )
    with pytest.raises(SnapshotImportRefused, match='canonical_name'):
        await importer.import_snapshot()


def test_eval_route_absent_when_eval_mode_off(client: Any) -> None:
    """The eval-mode test fixture starts the server WITHOUT eval_mode set,
    so the snapshot-import route must not be reachable."""
    response = client.post(
        '/api/v1/_eval/snapshot-import',
        json={'snapshot_path': '/tmp/whatever', 'target_vault_name': 'x'},
    )
    # 404 (route not registered) — NOT 400/403/422.
    assert response.status_code == 404


async def test_import_skips_missing_optional_files(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
    eval_state_table: None,
) -> None:
    """Missing derived/note_appends.jsonl etc. is treated as zero rows."""
    src_vault = populated_vault['vault_id']
    snapshot_dir = tmp_path / 'snapshot'
    snapshot_dir.mkdir()
    await _export(db_session, src_vault, snapshot_dir)
    await _delete_source_vault(db_session, src_vault)

    # Remove an optional file that the populated_vault never wrote anyway.
    optional = snapshot_dir / 'derived' / 'note_appends.jsonl'
    if optional.exists():
        optional.unlink()

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=snapshot_dir,
        allowlist_root=tmp_path,
        target_vault_name='missing-optional',
    )
    target_vault_id = await importer.import_snapshot()

    note_appends = (
        await db_session.execute(
            select(func.count())
            .select_from(NoteAppend)
            .join(Note, Note.id == NoteAppend.note_id)
            .where(Note.vault_id == target_vault_id)
        )
    ).scalar_one()
    assert note_appends == 0
