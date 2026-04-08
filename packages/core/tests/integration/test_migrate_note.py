"""Integration tests for NoteService.migrate_note.

Requires Docker (testcontainers PostgreSQL).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.exceptions import NoteNotFoundError, VaultNotFoundError
from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    Chunk,
    ContentStatus,
    Entity,
    EntityCooccurrence,
    MentalModel,
    MemoryLink,
    MemoryUnit,
    Node,
    Note,
    UnitEntity,
    Vault,
)
from memex_core.services.notes import NoteService
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EMBEDDING = [0.1] * 384
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fresh_get(session: AsyncSession, model, pk: UUID):
    """Fetch a row from DB, overwriting any stale identity-map entry."""
    result = await session.exec(
        select(model).where(col(model.id) == pk).execution_options(populate_existing=True)
    )
    return result.first()


async def _fresh_query(session: AsyncSession, stmt):
    """Run a SELECT that overwrites stale identity-map entries."""
    return await session.exec(stmt.execution_options(populate_existing=True))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def target_vault(session) -> Vault:
    """Create a second vault to migrate into."""
    vault = Vault(id=uuid4(), name=f'target-{uuid4().hex[:8]}', description='Migration target')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


@pytest_asyncio.fixture
async def source_vault(session) -> Vault:
    """Use global vault as source, return it for reference."""
    vault = await session.get(Vault, GLOBAL_VAULT_ID)
    assert vault is not None
    return vault


@pytest.fixture
def svc(metastore: AsyncPostgresMetaStoreEngine, filestore: BaseAsyncFileStore) -> NoteService:
    """NoteService wired to real metastore and filestore."""
    from unittest.mock import MagicMock

    from memex_core.services.vaults import VaultService

    config = MagicMock()
    vaults = VaultService(metastore=metastore, filestore=filestore, config=config)
    return NoteService(metastore=metastore, filestore=filestore, config=config, vaults=vaults)


async def _seed_full_note(
    session,
    vault: Vault,
    *,
    n_units: int = 2,
    with_entities: bool = True,
    with_links: bool = True,
    with_chunks: bool = True,
    with_nodes: bool = True,
    with_mental_models: bool = True,
    with_cooccurrences: bool = True,
) -> dict:
    """Insert a complete Note with all child records and return references."""
    note_id = uuid4()
    vault_id = vault.id
    vault_name = vault.name

    note = Note(
        id=note_id,
        vault_id=vault_id,
        original_text=f'Test content {uuid4().hex}',
        content_hash=uuid4().hex,
        filestore_path=f'assets/{vault_name}/{note_id}',
        assets=[
            f'assets/{vault_name}/{note_id}/image.png',
            f'assets/{vault_name}/{note_id}/doc.pdf',
        ],
        title='Migration Test Note',
    )
    session.add(note)

    chunks = []
    if with_chunks:
        for i in range(n_units):
            chunk = Chunk(
                id=uuid4(),
                vault_id=vault_id,
                note_id=note_id,
                text=f'Chunk {i} text',
                content_hash=uuid4().hex,
                embedding=EMBEDDING,
                chunk_index=i,
                status=ContentStatus.ACTIVE,
            )
            session.add(chunk)
            chunks.append(chunk)

    nodes = []
    if with_nodes and chunks:
        for i, chunk in enumerate(chunks):
            node = Node(
                id=uuid4(),
                vault_id=vault_id,
                note_id=note_id,
                block_id=chunk.id,
                node_hash=uuid4().hex,
                title=f'Section {i}',
                text=f'Node {i} text',
                level=1,
                seq=i,
                token_estimate=10,
                status=ContentStatus.ACTIVE,
            )
            session.add(node)
            nodes.append(node)

    units = []
    for i in range(n_units):
        unit = MemoryUnit(
            id=uuid4(),
            vault_id=vault_id,
            note_id=note_id,
            chunk_id=chunks[i].id if chunks and i < len(chunks) else None,
            text=f'Extracted fact {i} {uuid4().hex}',
            fact_type=FactTypes.WORLD,
            embedding=EMBEDDING,
            event_date=NOW,
            status=ContentStatus.ACTIVE,
        )
        session.add(unit)
        units.append(unit)

    entities = []
    unit_entities = []
    if with_entities:
        for i in range(min(n_units, 3)):
            entity = Entity(
                id=uuid4(),
                canonical_name=f'Entity-{uuid4().hex[:8]}',
                mention_count=1,
            )
            session.add(entity)
            entities.append(entity)
        await session.flush()

        for i, unit in enumerate(units):
            ue = UnitEntity(
                unit_id=unit.id,
                entity_id=entities[i % len(entities)].id,
                vault_id=vault_id,
            )
            session.add(ue)
            unit_entities.append(ue)

    links = []
    if with_links and len(units) >= 2:
        link = MemoryLink(
            from_unit_id=units[0].id,
            to_unit_id=units[1].id,
            vault_id=vault_id,
            link_type='semantic',
            weight=1.0,
        )
        session.add(link)
        links.append(link)

    mental_models = []
    if with_mental_models and entities:
        for entity in entities:
            mm = MentalModel(
                id=uuid4(),
                vault_id=vault_id,
                entity_id=entity.id,
                name=entity.canonical_name,
                observations=[],
                last_refreshed=NOW,
            )
            session.add(mm)
            mental_models.append(mm)

    cooccurrences = []
    if with_cooccurrences and len(entities) >= 2:
        sorted_ids = sorted([entities[0].id, entities[1].id])
        co = EntityCooccurrence(
            entity_id_1=sorted_ids[0],
            entity_id_2=sorted_ids[1],
            vault_id=vault_id,
            cooccurrence_count=5,
            last_cooccurred=NOW,
        )
        session.add(co)
        cooccurrences.append(co)

    await session.commit()

    return {
        'note': note,
        'chunks': chunks,
        'nodes': nodes,
        'units': units,
        'entities': entities,
        'unit_entities': unit_entities,
        'links': links,
        'mental_models': mental_models,
        'cooccurrences': cooccurrences,
    }


# ---------------------------------------------------------------------------
# Happy path — full migration
# ---------------------------------------------------------------------------


async def test_migrate_note_updates_all_vault_ids(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """All child records have their vault_id updated to the target vault."""
    data = await _seed_full_note(session, source_vault)
    note = data['note']
    target_id = target_vault.id

    result = await svc.migrate_note(note.id, target_id)

    assert result['status'] == 'success'
    assert result['target_vault_id'] == str(target_id)

    # Re-fetch every record from DB (bypassing session identity map cache)
    refreshed_note = await _fresh_get(session, Note, note.id)
    assert refreshed_note is not None
    assert refreshed_note.vault_id == target_id

    for chunk in data['chunks']:
        c = await _fresh_get(session, Chunk, chunk.id)
        assert c.vault_id == target_id, f'Chunk {chunk.id} vault_id not updated'

    for node in data['nodes']:
        n = await _fresh_get(session, Node, node.id)
        assert n.vault_id == target_id, f'Node {node.id} vault_id not updated'

    for unit in data['units']:
        u = await _fresh_get(session, MemoryUnit, unit.id)
        assert u.vault_id == target_id, f'MemoryUnit {unit.id} vault_id not updated'

    for ue in data['unit_entities']:
        row = (
            await _fresh_query(
                session,
                select(UnitEntity).where(
                    col(UnitEntity.unit_id) == ue.unit_id,
                    col(UnitEntity.entity_id) == ue.entity_id,
                ),
            )
        ).first()
        assert row is not None
        assert row.vault_id == target_id, f'UnitEntity {ue.unit_id} vault_id not updated'

    for link in data['links']:
        row = (
            await _fresh_query(
                session,
                select(MemoryLink).where(
                    col(MemoryLink.from_unit_id) == link.from_unit_id,
                    col(MemoryLink.to_unit_id) == link.to_unit_id,
                    col(MemoryLink.link_type) == link.link_type,
                ),
            )
        ).first()
        assert row is not None
        assert row.vault_id == target_id, 'MemoryLink vault_id not updated'


async def test_migrate_note_rewrites_filestore_paths(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """filestore_path and assets strings are rewritten to target vault name."""
    data = await _seed_full_note(session, source_vault)
    note = data['note']

    await svc.migrate_note(note.id, target_vault.id)

    refreshed = await _fresh_get(session, Note, note.id)
    expected_prefix = f'assets/{target_vault.name}/{note.id}'
    assert refreshed.filestore_path == expected_prefix
    assert refreshed.assets == [
        f'{expected_prefix}/image.png',
        f'{expected_prefix}/doc.pdf',
    ]


async def test_migrate_note_returns_entities_affected(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """Return value includes correct entities_affected count."""
    data = await _seed_full_note(session, source_vault, n_units=3)

    result = await svc.migrate_note(data['note'].id, target_vault.id)

    # We created min(3, 3) = 3 entities
    assert result['entities_affected'] == len(data['entities'])


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------


async def test_migrate_note_deletes_orphaned_mental_models(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """Mental models in source vault are deleted when the entity has no remaining units."""
    data = await _seed_full_note(session, source_vault, n_units=1)
    entity = data['entities'][0]

    # Verify mental model exists before migration
    mm_before = (
        await _fresh_query(
            session,
            select(MentalModel).where(
                col(MentalModel.entity_id) == entity.id,
                col(MentalModel.vault_id) == source_vault.id,
            ),
        )
    ).first()
    assert mm_before is not None

    await svc.migrate_note(data['note'].id, target_vault.id)

    # Mental model should be gone from source vault
    mm_after = (
        await _fresh_query(
            session,
            select(MentalModel).where(
                col(MentalModel.entity_id) == entity.id,
                col(MentalModel.vault_id) == source_vault.id,
            ),
        )
    ).first()
    assert mm_after is None


async def test_migrate_note_preserves_mental_models_with_remaining_units(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """Mental models are kept when the entity still has units in the source vault."""
    # Seed two notes that share an entity
    data1 = await _seed_full_note(session, source_vault, n_units=1)
    entity = data1['entities'][0]

    # Second note linking to the same entity
    note2_id = uuid4()
    note2 = Note(
        id=note2_id,
        vault_id=source_vault.id,
        original_text=f'Second note {uuid4().hex}',
        content_hash=uuid4().hex,
        title='Second note',
    )
    session.add(note2)
    unit2 = MemoryUnit(
        id=uuid4(),
        vault_id=source_vault.id,
        note_id=note2_id,
        text=f'Fact from second note {uuid4().hex}',
        fact_type=FactTypes.WORLD,
        embedding=EMBEDDING,
        event_date=NOW,
        status=ContentStatus.ACTIVE,
    )
    session.add(unit2)
    await session.flush()

    ue2 = UnitEntity(
        unit_id=unit2.id,
        entity_id=entity.id,
        vault_id=source_vault.id,
    )
    session.add(ue2)
    await session.commit()

    # Migrate only note1
    await svc.migrate_note(data1['note'].id, target_vault.id)

    # Mental model should survive because note2's unit still references the entity
    mm_after = (
        await _fresh_query(
            session,
            select(MentalModel).where(
                col(MentalModel.entity_id) == entity.id,
                col(MentalModel.vault_id) == source_vault.id,
            ),
        )
    ).first()
    assert mm_after is not None


async def test_migrate_note_deletes_cooccurrences_in_source_vault(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """EntityCooccurrence rows in the source vault are deleted for affected entities."""
    data = await _seed_full_note(session, source_vault, n_units=2)

    # Verify cooccurrences exist before migration
    co_before = (
        await _fresh_query(
            session,
            select(EntityCooccurrence).where(col(EntityCooccurrence.vault_id) == source_vault.id),
        )
    ).all()
    assert len(co_before) > 0

    await svc.migrate_note(data['note'].id, target_vault.id)

    co_after = (
        await _fresh_query(
            session,
            select(EntityCooccurrence).where(col(EntityCooccurrence.vault_id) == source_vault.id),
        )
    ).all()
    assert len(co_after) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_migrate_note_without_memory_units(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """A note with no memory units migrates cleanly (no units/entities to update)."""
    data = await _seed_full_note(
        session,
        source_vault,
        n_units=0,
        with_entities=False,
        with_links=False,
        with_chunks=False,
        with_nodes=False,
        with_mental_models=False,
        with_cooccurrences=False,
    )

    result = await svc.migrate_note(data['note'].id, target_vault.id)

    assert result['status'] == 'success'
    assert result['entities_affected'] == 0

    refreshed = await _fresh_get(session, Note, data['note'].id)
    assert refreshed.vault_id == target_vault.id


async def test_migrate_note_without_filestore_path(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """A note with no filestore_path or assets migrates without errors."""
    note_id = uuid4()
    note = Note(
        id=note_id,
        vault_id=source_vault.id,
        original_text=f'Bare note {uuid4().hex}',
        content_hash=uuid4().hex,
        filestore_path=None,
        assets=[],
        title='Bare note',
    )
    session.add(note)
    await session.commit()

    result = await svc.migrate_note(note_id, target_vault.id)

    assert result['status'] == 'success'
    refreshed = await _fresh_get(session, Note, note_id)
    assert refreshed.filestore_path is None
    assert refreshed.assets == []


# ---------------------------------------------------------------------------
# Error conditions (real DB)
# ---------------------------------------------------------------------------


async def test_migrate_note_not_found_raises(svc: NoteService, target_vault: Vault):
    """NoteNotFoundError when the note doesn't exist."""
    with pytest.raises(NoteNotFoundError):
        await svc.migrate_note(uuid4(), target_vault.id)


async def test_migrate_same_vault_raises(svc: NoteService, session, source_vault: Vault):
    """ValueError when source and target vault are the same."""
    data = await _seed_full_note(
        session,
        source_vault,
        n_units=0,
        with_entities=False,
        with_links=False,
        with_chunks=False,
        with_nodes=False,
        with_mental_models=False,
        with_cooccurrences=False,
    )

    with pytest.raises(ValueError, match='same'):
        await svc.migrate_note(data['note'].id, source_vault.id)


async def test_migrate_target_vault_not_found_raises(
    svc: NoteService, session, source_vault: Vault
):
    """VaultNotFoundError when the target vault doesn't exist."""
    data = await _seed_full_note(
        session,
        source_vault,
        n_units=0,
        with_entities=False,
        with_links=False,
        with_chunks=False,
        with_nodes=False,
        with_mental_models=False,
        with_cooccurrences=False,
    )

    with pytest.raises(VaultNotFoundError):
        await svc.migrate_note(data['note'].id, uuid4())


# ---------------------------------------------------------------------------
# Idempotency / double migration
# ---------------------------------------------------------------------------


async def test_migrate_back_and_forth(
    svc: NoteService, session, source_vault: Vault, target_vault: Vault
):
    """Migrating A->B then B->A restores original state."""
    data = await _seed_full_note(
        session,
        source_vault,
        n_units=1,
        with_mental_models=False,
        with_cooccurrences=False,
    )
    note_id = data['note'].id
    original_path = data['note'].filestore_path
    original_assets = list(data['note'].assets)

    # Migrate to target
    r1 = await svc.migrate_note(note_id, target_vault.id)
    assert r1['status'] == 'success'

    # Migrate back
    r2 = await svc.migrate_note(note_id, source_vault.id)
    assert r2['status'] == 'success'

    refreshed = await _fresh_get(session, Note, note_id)
    assert refreshed.vault_id == source_vault.id
    assert refreshed.filestore_path == original_path
    assert refreshed.assets == original_assets

    # All child records should be back in source vault
    for unit in data['units']:
        u = await _fresh_get(session, MemoryUnit, unit.id)
        assert u.vault_id == source_vault.id

    for ue in data['unit_entities']:
        row = (
            await _fresh_query(
                session,
                select(UnitEntity).where(
                    col(UnitEntity.unit_id) == ue.unit_id,
                    col(UnitEntity.entity_id) == ue.entity_id,
                ),
            )
        ).first()
        assert row.vault_id == source_vault.id


# ---------------------------------------------------------------------------
# Cross-vault evidence pruning during migration
# ---------------------------------------------------------------------------


async def _seed_note_with_evidence(
    session: AsyncSession,
    vault: Vault,
    entity: Entity,
    *,
    n_units: int = 2,
) -> dict:
    """Seed a note whose units are linked to *entity*, returning references.

    Unlike ``_seed_full_note`` this does NOT create its own entities or mental
    models — callers wire those up explicitly so evidence references are
    controllable.
    """
    note_id = uuid4()
    vault_id = vault.id

    note = Note(
        id=note_id,
        vault_id=vault_id,
        original_text=f'Evidence test note {uuid4().hex}',
        content_hash=uuid4().hex,
        title='Evidence test note',
    )
    session.add(note)

    units: list[MemoryUnit] = []
    for i in range(n_units):
        unit = MemoryUnit(
            id=uuid4(),
            vault_id=vault_id,
            note_id=note_id,
            text=f'Extracted fact {i} {uuid4().hex}',
            fact_type=FactTypes.WORLD,
            embedding=EMBEDDING,
            event_date=NOW,
            status=ContentStatus.ACTIVE,
        )
        session.add(unit)
        units.append(unit)

    await session.flush()

    for unit in units:
        ue = UnitEntity(
            unit_id=unit.id,
            entity_id=entity.id,
            vault_id=vault_id,
        )
        session.add(ue)

    await session.commit()
    return {'note': note, 'units': units}


def _make_observation(unit_ids: list[UUID]) -> dict:
    """Build a serialised Observation dict with evidence pointing to *unit_ids*."""
    from memex_core.memory.sql_models import EvidenceItem, Observation

    evidence = [EvidenceItem(memory_id=uid, quote='test quote', relevance=1.0) for uid in unit_ids]
    obs = Observation(
        title='Test observation',
        content='Observation content',
        evidence=evidence,
    )
    return obs.model_dump(mode='json')


async def test_migrate_note_prunes_evidence_from_surviving_models(
    svc: NoteService, session: AsyncSession, source_vault: Vault, target_vault: Vault
):
    """AC-001: Surviving mental models in the source vault have stale evidence pruned."""
    # Shared entity
    entity = Entity(id=uuid4(), canonical_name=f'Entity-{uuid4().hex[:8]}', mention_count=2)
    session.add(entity)
    await session.flush()

    # Two notes for the same entity in the source vault
    data1 = await _seed_note_with_evidence(session, source_vault, entity, n_units=2)
    data2 = await _seed_note_with_evidence(session, source_vault, entity, n_units=2)

    # Mental model citing units from BOTH notes
    note1_unit_ids = [u.id for u in data1['units']]
    note2_unit_ids = [u.id for u in data2['units']]
    obs1 = _make_observation(note1_unit_ids)
    obs2 = _make_observation(note2_unit_ids)

    mm = MentalModel(
        id=uuid4(),
        vault_id=source_vault.id,
        entity_id=entity.id,
        name=entity.canonical_name,
        observations=[obs1, obs2],
        last_refreshed=NOW,
    )
    session.add(mm)
    await session.commit()

    # Migrate note 1 -> target vault
    await svc.migrate_note(data1['note'].id, target_vault.id)

    # Reload mental model
    refreshed_mm = await _fresh_get(session, MentalModel, mm.id)
    assert refreshed_mm is not None, 'Model should survive (note 2 still in source vault)'

    # Evidence from note 1 should be gone, note 2 evidence intact
    from memex_core.memory.sql_models import Observation as ObsModel

    remaining_obs = [ObsModel(**o) for o in refreshed_mm.observations]
    remaining_evidence_ids = {ev.memory_id for obs in remaining_obs for ev in obs.evidence}

    for uid in note1_unit_ids:
        assert uid not in remaining_evidence_ids, f'Unit {uid} from note 1 should be pruned'
    for uid in note2_unit_ids:
        assert uid in remaining_evidence_ids, f'Unit {uid} from note 2 should be intact'


async def test_migrate_note_deletes_model_when_all_evidence_migrated(
    svc: NoteService, session: AsyncSession, source_vault: Vault, target_vault: Vault
):
    """AC-002: Model deleted when all its evidence came from migrated note."""
    entity = Entity(id=uuid4(), canonical_name=f'Entity-{uuid4().hex[:8]}', mention_count=2)
    session.add(entity)
    await session.flush()

    # Two notes for the same entity so the entity survives in source vault
    data1 = await _seed_note_with_evidence(session, source_vault, entity, n_units=2)
    await _seed_note_with_evidence(session, source_vault, entity, n_units=1)

    # Mental model only cites note 1's units
    note1_unit_ids = [u.id for u in data1['units']]
    obs = _make_observation(note1_unit_ids)

    mm = MentalModel(
        id=uuid4(),
        vault_id=source_vault.id,
        entity_id=entity.id,
        name=entity.canonical_name,
        observations=[obs],
        last_refreshed=NOW,
    )
    session.add(mm)
    await session.commit()

    # Migrate note 1
    await svc.migrate_note(data1['note'].id, target_vault.id)

    # Entity still has units in source vault (from note 2), but model should be deleted
    # because all observations lost all evidence
    refreshed_mm = await _fresh_get(session, MentalModel, mm.id)
    assert refreshed_mm is None, 'Model should be deleted when all evidence is pruned'

    # Verify entity still has units in source vault
    remaining = (
        await _fresh_query(
            session,
            select(UnitEntity.unit_id).where(
                col(UnitEntity.entity_id) == entity.id,
                col(UnitEntity.vault_id) == source_vault.id,
            ),
        )
    ).all()
    assert len(remaining) > 0, 'Entity should still have units from note 2'


async def test_migrate_note_leaves_unrelated_evidence_intact(
    svc: NoteService, session: AsyncSession, source_vault: Vault, target_vault: Vault
):
    """AC-003: Mental models citing only non-migrated notes are unaffected."""
    entity = Entity(id=uuid4(), canonical_name=f'Entity-{uuid4().hex[:8]}', mention_count=2)
    session.add(entity)
    await session.flush()

    # Two notes for the same entity
    data1 = await _seed_note_with_evidence(session, source_vault, entity, n_units=1)
    data2 = await _seed_note_with_evidence(session, source_vault, entity, n_units=2)

    # Mental model only cites note 2's units
    note2_unit_ids = [u.id for u in data2['units']]
    obs = _make_observation(note2_unit_ids)

    mm = MentalModel(
        id=uuid4(),
        vault_id=source_vault.id,
        entity_id=entity.id,
        name=entity.canonical_name,
        observations=[obs],
        last_refreshed=NOW,
    )
    session.add(mm)
    await session.commit()

    # Capture original observations for comparison
    original_observations = list(mm.observations)

    # Migrate note 1 (model only cites note 2 — should be unaffected)
    await svc.migrate_note(data1['note'].id, target_vault.id)

    refreshed_mm = await _fresh_get(session, MentalModel, mm.id)
    assert refreshed_mm is not None, 'Model should survive'
    assert refreshed_mm.observations == original_observations, 'Observations should be unchanged'
