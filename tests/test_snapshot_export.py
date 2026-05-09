"""Integration tests for V3 vault snapshot export.

Exercises the SnapshotExporter end-to-end against a testcontainer
Postgres: build a vault with notes + chunks + units + entities + links
+ a mental model + a vault summary + a maintenance proposal, run the
exporter, and assert the output layout and per-table row counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
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
    SNAPSHOT_VERSION,
    SnapshotExporter,
    SnapshotManifest,
)
from memex_core.services.snapshot.exporter import SnapshotExportError
from memex_core.services.snapshot.manifest import EmbeddingModelIdentity


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ----------------------------------------------------------------------
# Fixture: a populated vault with at least one row in every restored table.


@pytest_asyncio.fixture
async def populated_vault(db_session: AsyncSession) -> dict[str, UUID]:
    vault = Vault(name='snapshot-test', description='Integration fixture vault')
    db_session.add(vault)
    await db_session.flush()

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        title='Project Alpha kickoff',
        description='First notes from the kickoff.',
        original_text='Project Alpha kicked off on 2026-05-01. Sarah Chen is the lead.',
        content_hash='abc123',
        status='active',
        doc_metadata={'source': 'manual'},
    )
    db_session.add(note)
    await db_session.flush()

    chunk = Chunk(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='Project Alpha kicked off.',
        content_hash='hash-1',
        chunk_index=0,
        embedding=[0.0] * 384,
    )
    db_session.add(chunk)
    await db_session.flush()

    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        chunk_id=chunk.id,
        text='Sarah Chen leads Project Alpha.',
        fact_type='world',
        status='active',
        intent_class='durable',
        risk_class='none',
        confidence=0.95,
        confidence_evidence_count=0,
        event_date=datetime.now(timezone.utc),
        is_deprioritized=False,
        success_co_count=2,
        failure_co_count=0,
        embedding=[0.0] * 384,
    )
    db_session.add(unit)
    await db_session.flush()

    entity_a = Entity(
        id=uuid4(),
        canonical_name='Sarah Chen',
        entity_type='Person',
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    entity_b = Entity(
        id=uuid4(),
        canonical_name='Project Alpha',
        entity_type='Concept',
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(entity_a)
    db_session.add(entity_b)
    await db_session.flush()

    alias = EntityAlias(id=uuid4(), canonical_id=entity_a.id, name='S. Chen', phonetic_code='SRXN')
    db_session.add(alias)

    db_session.add(
        UnitEntity(
            unit_id=unit.id,
            entity_id=entity_a.id,
            vault_id=vault.id,
        )
    )
    db_session.add(
        UnitEntity(
            unit_id=unit.id,
            entity_id=entity_b.id,
            vault_id=vault.id,
        )
    )

    # EntityCooccurrence requires entity_id_1 < entity_id_2.
    eid1, eid2 = sorted([entity_a.id, entity_b.id])
    db_session.add(
        EntityCooccurrence(
            entity_id_1=eid1,
            entity_id_2=eid2,
            vault_id=vault.id,
            cooccurrence_count=3,
        )
    )

    # Second unit so we can build a link.
    unit2 = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        chunk_id=chunk.id,
        text='Project Alpha targets Q3.',
        fact_type='world',
        status='active',
        intent_class='durable',
        risk_class='none',
        confidence=0.9,
        confidence_evidence_count=0,
        event_date=datetime.now(timezone.utc),
        embedding=[0.0] * 384,
    )
    db_session.add(unit2)
    await db_session.flush()

    db_session.add(
        MemoryLink(
            from_unit_id=unit.id,
            to_unit_id=unit2.id,
            link_type='semantic',
            vault_id=vault.id,
            entity_id=entity_b.id,
            weight=0.8,
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
                    'title': 'Leads Alpha',
                    'content': 'Sarah Chen is the lead for Project Alpha.',
                    'trend': 'new',
                    'evidence': [],
                }
            ],
            entity_metadata={'role': 'lead'},
            last_refreshed=datetime.now(timezone.utc),
            version=1,
        )
    )

    db_session.add(
        VaultSummary(
            vault_id=vault.id,
            narrative='Notes about Project Alpha',
            themes=[{'name': 'project'}],
            inventory={'total_notes': 1},
            key_entities=[{'name': 'Sarah Chen', 'type': 'Person', 'mention_count': 1}],
            version=1,
            notes_incorporated=1,
        )
    )

    db_session.add(
        MaintenanceProposal(
            id=uuid4(),
            vault_id=vault.id,
            lint_type='quality',
            target_type='memory_unit',
            target_id=str(unit.id),
            rule_name='probe',
            evidence={'reason': 'fixture'},
            suggested_action='no-op',
            status='pending',
            source='rule',
        )
    )

    db_session.add(
        NoteAppend(
            append_id=uuid4(),
            note_id=note.id,
            delta_sha256='deadbeef',
            delta_bytes=12,
            joiner='\n\n',
            resulting_content_hash='cafebabe',
        )
    )

    await db_session.commit()

    return {
        'vault_id': vault.id,
        'vault_name': vault.name,
        'note_id': note.id,
        'unit_id': unit.id,
        'entity_a_id': entity_a.id,
        'entity_b_id': entity_b.id,
    }


# ----------------------------------------------------------------------
# Tests


async def test_export_writes_expected_layout(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    out = tmp_path / 'snap'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,  # No assets in fixture.
        vault_id_or_name=populated_vault['vault_name'],
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='test/embedder', dim=384, hash='deadbeef'),
    )
    manifest = await exporter.export()

    # Top-level files and directories.
    assert (out / 'manifest.json').is_file()
    assert (out / 'vault.json').is_file()
    assert (out / 'README.md').is_file()
    assert (out / 'notes').is_dir()
    assert (out / 'derived').is_dir()
    assert (out / 'governance').is_dir()

    # Manifest invariants.
    assert manifest.snapshot_version == SNAPSHOT_VERSION
    assert manifest.source_vault_id == populated_vault['vault_id']
    assert manifest.source_vault_name == populated_vault['vault_name']
    assert manifest.embedding_model.name == 'test/embedder'
    assert manifest.embedding_model.dim == 384
    assert manifest.observation_schema_version == '1'
    assert manifest.alembic_head  # Not empty.

    # Manifest is the LAST file written; round-trip from disk to verify.
    on_disk = json.loads((out / 'manifest.json').read_text())
    re_parsed = SnapshotManifest.model_validate(on_disk)
    assert re_parsed.source_vault_id == populated_vault['vault_id']

    # Per-table counts match expected fixture rows.
    counts = manifest.table_counts
    assert counts.get('notes') == 1
    assert counts.get('chunks') == 1
    assert counts.get('memory_units') == 2
    assert counts.get('unit_entities') == 2
    assert counts.get('entities') == 2  # UNION reference set.
    assert counts.get('entity_aliases') == 1
    assert counts.get('entity_cooccurrences') == 1
    assert counts.get('memory_links') == 1
    assert counts.get('vault_summaries') == 1
    assert counts.get('maintenance_proposals') == 1
    assert counts.get('note_appends') == 1


async def test_export_excludes_other_vaults(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Per-table vault scope: a second vault's rows must NOT leak into
    the export of the first vault.
    """
    other = Vault(name='other-vault')
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Note(
            id=uuid4(),
            vault_id=other.id,
            title='Should not appear',
            original_text='Leakage canary.',
            content_hash='other-hash',
            status='active',
        )
    )
    await db_session.commit()

    out = tmp_path / 'snap-leak'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=populated_vault['vault_name'],
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    manifest = await exporter.export()

    # Only one note expected (the populated_vault one); the other vault's
    # note must not appear.
    assert manifest.table_counts['notes'] == 1
    note_dirs = list((out / 'notes').iterdir())
    assert len(note_dirs) == 1
    metadata = json.loads((note_dirs[0] / 'metadata.json').read_text())
    assert UUID(metadata['vault_id']) == populated_vault['vault_id']


async def test_export_refuses_global_vault(db_session: AsyncSession, tmp_path: Path) -> None:
    out = tmp_path / 'snap-global'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=GLOBAL_VAULT_ID,
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    with pytest.raises(SnapshotExportError, match='global'):
        await exporter.export()


async def test_export_refuses_reserved_name(db_session: AsyncSession, tmp_path: Path) -> None:
    danger = Vault(name='default')
    db_session.add(danger)
    await db_session.commit()

    out = tmp_path / 'snap-reserved'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name='default',
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    with pytest.raises(SnapshotExportError, match='reserved'):
        await exporter.export()


async def test_export_refuses_unknown_vault(db_session: AsyncSession, tmp_path: Path) -> None:
    out = tmp_path / 'snap-unknown'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name='no-such-vault',
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    with pytest.raises(SnapshotExportError, match='not found'):
        await exporter.export()


async def test_jsonl_lines_are_valid_json(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    out = tmp_path / 'snap-jsonl'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=populated_vault['vault_name'],
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    await exporter.export()

    for jsonl in (out / 'derived').glob('*.jsonl'):
        with jsonl.open() as fp:
            for lineno, line in enumerate(fp, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    pytest.fail(f'{jsonl.name}:{lineno} not valid JSON: {e}')


async def test_export_excludes_embedding_columns(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Embedding columns leak 3-5KB per row of opaque float lists; V3
    explicitly drops them. The snapshot must not contain them.
    """
    out = tmp_path / 'snap-noembed'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=populated_vault['vault_name'],
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    await exporter.export()

    chunks_jsonl = (out / 'derived' / 'chunks.jsonl').read_text()
    units_jsonl = (out / 'derived' / 'memory_units.jsonl').read_text()
    mentals_jsonl = (out / 'derived' / 'mental_models.jsonl').read_text()

    assert 'embedding' not in chunks_jsonl
    assert 'embedding' not in units_jsonl
    # Be precise: we don't want the column key, but ``entity_metadata`` is fine.
    for line in mentals_jsonl.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert 'embedding' not in record


async def test_entity_reference_set_is_union(
    db_session: AsyncSession,
    populated_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """The exported entities must be the UNION of references from
    unit_entities, memory_links, mental_models, and entity_cooccurrences
    — not just the unit_entities set.
    """
    out = tmp_path / 'snap-union'
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=populated_vault['vault_name'],
        output_dir=out,
        embedding_model=EmbeddingModelIdentity(name='t', dim=384),
    )
    await exporter.export()

    exported_entity_ids: set[str] = set()
    for line in (out / 'derived' / 'entities.jsonl').read_text().splitlines():
        if line.strip():
            exported_entity_ids.add(json.loads(line)['id'])

    # Both fixture entities should appear (entity_a is on a UnitEntity,
    # entity_b is on a UnitEntity AND a MemoryLink — both reachable via
    # different paths in the union).
    assert str(populated_vault['entity_a_id']) in exported_entity_ids
    assert str(populated_vault['entity_b_id']) in exported_entity_ids
