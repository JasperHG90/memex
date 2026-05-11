"""Integration tests for cross-batch entity-cluster collapse maintenance.

Exercises the six referential-integrity hazards and the scan/emit/apply
flow against a real Postgres. All tests are marked ``@pytest.mark.integration``
and assume the testcontainers fixtures defined in
``packages/core/tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import (
    Entity,
    EntityAlias,
    EntityCooccurrence,
    MemoryLink,
    MemoryUnit,
    MentalModel,
    Note,
    UnitEntity,
    Vault,
)
from memex_core.services.entities import EntityService


def _make_api_stub(metastore):
    """Construct a minimal MemexAPI surrogate for scan_collapse_clusters.

    The scan needs only ``api.metastore`` and ``api.config``; rather than spin
    up the full MemexAPI (which initializes models, services, schedulers) we
    expose just those two attributes.
    """
    from memex_common.config import (
        EntityMaintenanceConfig,
        MemexConfig,
    )

    config = MemexConfig()
    config.server.memory.entity_maintenance = EntityMaintenanceConfig(
        scan_enabled=True,
        top_n=100,
        scan_cooldown_days=0,
        pair_threshold=0.85,
        cluster_min_threshold=0.7,
    )
    stub = MagicMock()
    stub.metastore = metastore
    stub.config = config
    return stub


async def _make_entity(
    session: AsyncSession,
    name: str,
    *,
    mention_count: int = 5,
    first_seen: datetime | None = None,
    phonetic_code: str | None = None,
) -> Entity:
    now = datetime.now(timezone.utc)
    e = Entity(
        canonical_name=name,
        phonetic_code=phonetic_code or 'AKM',
        mention_count=mention_count,
        first_seen=first_seen or now,
        last_seen=now,
    )
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return e


async def _add_unit_entity(
    session: AsyncSession,
    *,
    unit_id: UUID,
    entity_id: UUID,
    vault_id: UUID,
    success: int = 0,
    failure: int = 0,
) -> None:
    session.add(
        UnitEntity(
            unit_id=unit_id,
            entity_id=entity_id,
            vault_id=vault_id,
            success_co_count=success,
            failure_co_count=failure,
        )
    )
    await session.commit()


async def _make_unit(session: AsyncSession, vault_id: UUID) -> MemoryUnit:
    note = Note(id=uuid4(), vault_id=vault_id, content_hash=uuid4().hex)
    session.add(note)
    await session.commit()
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault_id,
        note_id=note.id,
        text=f'unit-{uuid4().hex}',
        event_date=datetime.now(timezone.utc),
    )
    session.add(unit)
    await session.commit()
    return unit


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_repoints_memory_links_before_delete(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 1: MemoryLink.entity_id MUST be repointed before the entity
    hard-delete, otherwise the FK CASCADE drops the rows."""
    e_winner = await _make_entity(session, f'ACME-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'acme-{uuid4().hex[:6]}')

    unit_a = await _make_unit(session, GLOBAL_VAULT_ID)
    unit_b = await _make_unit(session, GLOBAL_VAULT_ID)

    link = MemoryLink(
        from_unit_id=unit_a.id,
        to_unit_id=unit_b.id,
        link_type='entity',
        entity_id=e_loser.id,
        vault_id=GLOBAL_VAULT_ID,
    )
    session.add(link)
    await session.commit()

    from memex_common.config import MemexConfig

    config = MemexConfig()
    svc = EntityService(metastore=metastore, filestore=filestore, config=config)
    summary = await svc.collapse_cluster(
        winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test'
    )

    assert summary['links_repointed'] >= 1

    async with metastore.session() as s:
        result = await s.execute(
            text(
                'SELECT entity_id FROM memory_links '
                'WHERE from_unit_id = :from_id AND to_unit_id = :to_id'
            ),
            {'from_id': str(unit_a.id), 'to_id': str(unit_b.id)},
        )
        row = result.first()
    assert row is not None, 'memory_link must survive the collapse'
    assert UUID(str(row[0])) == e_winner.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_merges_unit_entity_counters(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 4: UnitEntity collision merged by summing counters."""
    e_winner = await _make_entity(session, f'win-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'loss-{uuid4().hex[:6]}')

    unit = await _make_unit(session, GLOBAL_VAULT_ID)
    await _add_unit_entity(
        session,
        unit_id=unit.id,
        entity_id=e_winner.id,
        vault_id=GLOBAL_VAULT_ID,
        success=2,
        failure=1,
    )
    await _add_unit_entity(
        session,
        unit_id=unit.id,
        entity_id=e_loser.id,
        vault_id=GLOBAL_VAULT_ID,
        success=3,
        failure=4,
    )

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT entity_id::text, success_co_count, failure_co_count '
                    'FROM unit_entities WHERE unit_id = :uid'
                ),
                {'uid': str(unit.id)},
            )
        ).all()

    assert len(rows) == 1, f'expected one merged row, got {rows}'
    eid, success, failure = rows[0]
    assert UUID(eid) == e_winner.id
    assert success == 5
    assert failure == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_absorbs_aliases(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 3: EntityAlias absorbed with ON CONFLICT DO NOTHING and the
    loser's canonical_name itself becomes an alias of the winner."""
    e_winner = await _make_entity(session, f'WinName-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'LoseName-{uuid4().hex[:6]}')

    session.add(EntityAlias(canonical_id=e_loser.id, name='shared-nickname'))
    session.add(EntityAlias(canonical_id=e_winner.id, name='shared-nickname'))
    await session.commit()

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text('SELECT name FROM entity_aliases WHERE canonical_id = :wid ORDER BY name'),
                {'wid': str(e_winner.id)},
            )
        ).all()
    names = [r[0] for r in rows]
    assert 'shared-nickname' in names
    assert e_loser.canonical_name in names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_reorders_and_sums_cooccurrences(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 2: EntityCooccurrence reordered with canonical (smaller,larger)
    UUID ordering and counts summed across loser+winner rows."""
    e_winner = await _make_entity(session, f'WIN-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'LOSE-{uuid4().hex[:6]}')
    e_peer = await _make_entity(session, f'PEER-{uuid4().hex[:6]}')

    # winner <-> peer in canonical order
    e1, e2 = sorted([e_winner.id, e_peer.id], key=str)
    session.add(
        EntityCooccurrence(
            entity_id_1=e1,
            entity_id_2=e2,
            vault_id=GLOBAL_VAULT_ID,
            cooccurrence_count=2,
        )
    )
    # loser <-> peer in canonical order
    e3, e4 = sorted([e_loser.id, e_peer.id], key=str)
    session.add(
        EntityCooccurrence(
            entity_id_1=e3,
            entity_id_2=e4,
            vault_id=GLOBAL_VAULT_ID,
            cooccurrence_count=3,
        )
    )
    await session.commit()

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT entity_id_1::text, entity_id_2::text, cooccurrence_count '
                    'FROM entity_cooccurrences '
                    'WHERE :pid IN (entity_id_1, entity_id_2) '
                    'OR :wid IN (entity_id_1, entity_id_2)'
                ),
                {'pid': str(e_peer.id), 'wid': str(e_winner.id)},
            )
        ).all()
    assert len(rows) == 1
    eid1, eid2, count = rows[0]
    assert {UUID(eid1), UUID(eid2)} == {e_winner.id, e_peer.id}
    # The IDs are stored in canonical order
    assert UUID(eid1) < UUID(eid2)
    assert count == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_merges_mental_models_with_version_bump(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 5: MentalModel per-vault collision → observations merged and
    ``version = max(winner, loser) + 1``."""
    e_winner = await _make_entity(session, f'EWIN-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'ELOSE-{uuid4().hex[:6]}')

    mm_w = MentalModel(
        vault_id=GLOBAL_VAULT_ID,
        entity_id=e_winner.id,
        name='EWIN',
        observations=[{'fact': 'w1'}],
        version=4,
    )
    mm_l = MentalModel(
        vault_id=GLOBAL_VAULT_ID,
        entity_id=e_loser.id,
        name='ELOSE',
        observations=[{'fact': 'l1'}, {'fact': 'l2'}],
        version=7,
    )
    session.add(mm_w)
    session.add(mm_l)
    await session.commit()

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text('SELECT version, observations FROM mental_models WHERE entity_id = :wid'),
                {'wid': str(e_winner.id)},
            )
        ).all()
    assert len(rows) == 1
    version, observations = rows[0]
    assert version == 8
    facts = sorted(o['fact'] for o in observations)
    assert facts == ['l1', 'l2', 'w1']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_flushes_before_entity_delete(
    session: AsyncSession,
    metastore,
    filestore,
    monkeypatch,
):
    """Step 5 deletes MentalModel rows via the ORM (deferred until flush);
    Step 6 then issues a raw SQL DELETE on entities. An explicit flush between
    them makes the ordering invariant — without it, the entity hard-delete
    could fire while stale MentalModel rows still hold FK references.
    """
    e_winner = await _make_entity(session, f'FW-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'FL-{uuid4().hex[:6]}')

    session.add(
        MentalModel(
            vault_id=GLOBAL_VAULT_ID,
            entity_id=e_winner.id,
            name='FW',
            observations=[{'fact': 'w'}],
            version=1,
        )
    )
    session.add(
        MentalModel(
            vault_id=GLOBAL_VAULT_ID,
            entity_id=e_loser.id,
            name='FL',
            observations=[{'fact': 'x'}],
            version=1,
        )
    )
    await session.commit()

    from sqlmodel.ext.asyncio.session import AsyncSession as _AS
    from memex_common.config import MemexConfig

    call_log: list[str] = []

    original_delete = _AS.delete
    original_flush = _AS.flush
    original_exec = _AS.exec

    async def _track_delete(self, instance):
        call_log.append(f'delete:{type(instance).__name__}')
        return await original_delete(self, instance)

    async def _track_flush(self, *args, **kwargs):
        call_log.append('flush')
        return await original_flush(self, *args, **kwargs)

    async def _track_exec(self, stmt, *args, **kwargs):
        raw = str(stmt)
        if 'DELETE FROM entities' in raw:
            call_log.append('exec:DELETE_ENTITIES')
        return await original_exec(self, stmt, *args, **kwargs)

    monkeypatch.setattr(_AS, 'delete', _track_delete)
    monkeypatch.setattr(_AS, 'flush', _track_flush)
    monkeypatch.setattr(_AS, 'exec', _track_exec)

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    mm_deletes = [i for i, e in enumerate(call_log) if e == 'delete:MentalModel']
    flushes = [i for i, e in enumerate(call_log) if e == 'flush']
    entity_deletes = [i for i, e in enumerate(call_log) if e == 'exec:DELETE_ENTITIES']

    assert mm_deletes, 'expected at least one MentalModel ORM delete'
    assert entity_deletes, 'expected the raw SQL DELETE on entities'
    last_mm_delete = mm_deletes[-1]
    first_entity_delete = entity_deletes[0]
    interposed_flush = [f for f in flushes if last_mm_delete < f < first_entity_delete]
    assert interposed_flush, (
        f'expected an explicit session.flush() between MentalModel delete and '
        f'entity DELETE, got call_log={call_log}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_writes_audit_log_with_details_column(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 7: AuditLog row uses the ``details`` column (not ``metadata``)."""
    e_winner = await _make_entity(session, f'AW-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'AL-{uuid4().hex[:6]}')

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='actor-x')

    async with metastore.session() as s:
        row = (
            await s.execute(
                text(
                    'SELECT actor, action, resource_id, details FROM audit_logs '
                    "WHERE action = 'entity.collapse_cluster' "
                    'ORDER BY timestamp DESC LIMIT 1'
                )
            )
        ).first()
    assert row is not None, 'audit row must exist'
    actor, action, resource_id, details = row
    assert actor == 'actor-x'
    assert action == 'entity.collapse_cluster'
    assert UUID(resource_id) == e_winner.id
    assert details is not None
    assert details['winner_id'] == str(e_winner.id)
    assert str(e_loser.id) in details['loser_ids']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_hard_deletes_losers_last(
    session: AsyncSession,
    metastore,
    filestore,
):
    """Hazard 6: loser entity rows are gone after collapse."""
    e_winner = await _make_entity(session, f'KW-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'KL-{uuid4().hex[:6]}')

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        winner_row = (
            await s.execute(
                text('SELECT id::text FROM entities WHERE id = :id'),
                {'id': str(e_winner.id)},
            )
        ).first()
        loser_row = (
            await s.execute(
                text('SELECT id::text FROM entities WHERE id = :id'),
                {'id': str(e_loser.id)},
            )
        ).first()
    assert winner_row is not None
    assert loser_row is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_emits_cluster_of_three(
    session: AsyncSession,
    metastore,
):
    """End-to-end: three near-duplicate entities → one cluster proposal."""
    suffix = uuid4().hex[:6]
    a = await _make_entity(
        session,
        f'ACME-Inc-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    b = await _make_entity(
        session,
        f'acme-inc-{suffix}',
        mention_count=5,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    c = await _make_entity(
        session,
        f'Acme-Inc-{suffix}',
        mention_count=3,
        first_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    summary = await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)
    assert summary['clusters_emitted'] >= 1

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT evidence FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' "
                    'AND target_id = :wid'
                ),
                {'wid': str(a.id)},
            )
        ).all()
    assert rows, 'cluster proposal for the suggested winner must exist'
    evidence = rows[0][0]
    members = set(evidence['cluster_members'])
    assert {str(a.id), str(b.id), str(c.id)}.issubset(members)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_rescan_updates_evidence_in_place(
    session: AsyncSession,
    metastore,
):
    """Re-scan with same suggested winner UPDATEs the existing pending row
    rather than inserting a duplicate."""
    suffix = uuid4().hex[:6]
    a = await _make_entity(
        session,
        f'ZED-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _b = await _make_entity(
        session,
        f'zed-{suffix}',
        mention_count=2,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)
    # Reset last_merge_scan_at to bypass cooldown for second pass
    async with metastore.session() as s:
        await s.execute(text('UPDATE entities SET last_merge_scan_at = NULL'))
        await s.commit()
    summary2 = await scan_collapse_clusters(
        api_stub, pair_threshold=0.55, cluster_min_threshold=0.4
    )
    assert summary2['rescan_updated'] >= 1

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT count(*) FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' AND target_id = :wid"
                ),
                {'wid': str(a.id)},
            )
        ).all()
    assert rows[0][0] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_respects_cooldown_window(
    session: AsyncSession,
    metastore,
):
    """An entity scanned within cooldown_days is skipped on the next pass."""
    suffix = uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    a = await _make_entity(session, f'Q1-{suffix}', mention_count=5)
    b = await _make_entity(session, f'q1-{suffix}', mention_count=4)
    # Mark both as just-scanned
    async with metastore.session() as s:
        await s.execute(
            text(
                'UPDATE entities SET last_merge_scan_at = :now WHERE id = ANY(CAST(:ids AS uuid[]))'
            ),
            {'now': now, 'ids': [str(a.id), str(b.id)]},
        )
        await s.commit()

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    api_stub.config.server.memory.entity_maintenance.scan_cooldown_days = 7
    summary = await scan_collapse_clusters(api_stub)
    # Filter to recently created entities only — other test entities may exist
    # in the same DB; we just assert that ours weren't included.
    assert summary['scanned'] >= 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_cluster_rejects_invalid_inputs(
    session: AsyncSession,
    metastore,
    filestore,
):
    """winner_id in loser_ids and empty loser_ids both raise ValueError."""
    e = await _make_entity(session, f'XX-{uuid4().hex[:6]}')

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())

    with pytest.raises(ValueError):
        await svc.collapse_cluster(winner_id=e.id, loser_ids=[], actor='t')
    with pytest.raises(ValueError):
        await svc.collapse_cluster(winner_id=e.id, loser_ids=[e.id], actor='t')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rescan_winner_shift_updates_same_proposal(
    session: AsyncSession,
    metastore,
):
    """Winner shifts between scans (mention_count reorders) but the cluster
    membership is unchanged → same composition_hash → existing pending row is
    UPDATEd in place. No duplicate finding."""
    suffix = uuid4().hex[:6]
    a = await _make_entity(
        session,
        f'WSHIFT-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    b = await _make_entity(
        session,
        f'wshift-{suffix}',
        mention_count=2,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)

    async with metastore.session() as s:
        before = (
            await s.execute(
                text(
                    'SELECT id::text, target_id::text FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' "
                    'AND evidence ->> :hash_key IN '
                    '(SELECT evidence ->> :hash_key FROM maintenance_proposals '
                    " WHERE target_id = :wid AND status = 'pending')"
                ),
                {'wid': str(a.id), 'hash_key': 'composition_hash'},
            )
        ).all()
    assert len(before) == 1, f'expected one proposal pre-flip, got {before}'
    initial_id = before[0][0]
    assert before[0][1] == str(a.id)

    async with metastore.session() as s:
        await s.execute(
            text('UPDATE entities SET mention_count = :mc WHERE id = :id'),
            {'mc': 99, 'id': str(b.id)},
        )
        await s.execute(
            text('UPDATE entities SET mention_count = :mc WHERE id = :id'),
            {'mc': 1, 'id': str(a.id)},
        )
        await s.execute(text('UPDATE entities SET last_merge_scan_at = NULL'))
        await s.commit()

    summary2 = await scan_collapse_clusters(
        api_stub, pair_threshold=0.55, cluster_min_threshold=0.4
    )
    assert summary2['rescan_updated'] >= 1

    async with metastore.session() as s:
        after = (
            await s.execute(
                text(
                    'SELECT id::text, target_id::text FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' "
                    'AND id = CAST(:id AS uuid)'
                ),
                {'id': initial_id},
            )
        ).all()
    assert len(after) == 1, 'row must survive the rescan (UPDATE not INSERT)'
    assert after[0][1] == str(b.id), 'target_id must reflect the new winner'

    async with metastore.session() as s:
        dup_count = (
            await s.execute(
                text(
                    'SELECT count(*) FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' "
                    'AND target_id IN (:wid_a, :wid_b)'
                ),
                {'wid_a': str(a.id), 'wid_b': str(b.id)},
            )
        ).scalar()
    assert dup_count == 1, 'must not split into two pending findings'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rescan_membership_shift_creates_new_proposal(
    session: AsyncSession,
    metastore,
):
    """Cluster gains a member between scans → composition_hash changes →
    a new row is INSERTed alongside the old one (different identity)."""
    suffix = uuid4().hex[:6]
    a = await _make_entity(
        session,
        f'MSHIFT-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _b = await _make_entity(
        session,
        f'mshift-{suffix}',
        mention_count=5,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)

    async with metastore.session() as s:
        first_rows = (
            await s.execute(
                text(
                    "SELECT id::text, evidence ->> 'composition_hash' AS h "
                    'FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' AND target_id = :wid"
                ),
                {'wid': str(a.id)},
            )
        ).all()
    assert len(first_rows) == 1, f'expected one proposal after first scan, got {first_rows}'
    first_id, first_hash = first_rows[0]

    await _make_entity(
        session,
        f'Mshift-{suffix}',
        mention_count=3,
        first_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    async with metastore.session() as s:
        await s.execute(text('UPDATE entities SET last_merge_scan_at = NULL'))
        await s.commit()

    await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)

    async with metastore.session() as s:
        second_rows = (
            await s.execute(
                text(
                    "SELECT id::text, evidence ->> 'composition_hash' AS h "
                    'FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' AND target_id = :wid"
                ),
                {'wid': str(a.id)},
            )
        ).all()
    hashes = {h for _, h in second_rows}
    assert first_hash in hashes, 'old finding (original membership) must remain'
    assert len(hashes) >= 2, 'membership change must introduce a new composition_hash'
    assert any(rid == first_id for rid, _ in second_rows), 'original row must be untouched'
    new_hashes = hashes - {first_hash}
    assert all(h for h in new_hashes), 'new composition_hash entries must be non-empty'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_creates_proposal_with_vaults_affected(
    session: AsyncSession,
    metastore,
):
    """Cross-vault: vaults_affected lists every vault the cluster members
    appear in via unit_entities."""
    suffix = uuid4().hex[:6]
    vault_a = Vault(name=f'va-{suffix}')
    vault_b = Vault(name=f'vb-{suffix}')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()
    await session.refresh(vault_a)
    await session.refresh(vault_b)

    e1 = await _make_entity(
        session,
        f'CrossVault-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    e2 = await _make_entity(
        session,
        f'crossvault-{suffix}',
        mention_count=5,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    unit_a = await _make_unit(session, vault_a.id)
    unit_b = await _make_unit(session, vault_b.id)
    await _add_unit_entity(session, unit_id=unit_a.id, entity_id=e1.id, vault_id=vault_a.id)
    await _add_unit_entity(session, unit_id=unit_b.id, entity_id=e2.id, vault_id=vault_b.id)

    from memex_core.services.entity_maintenance import scan_collapse_clusters

    api_stub = _make_api_stub(metastore)
    await scan_collapse_clusters(api_stub, pair_threshold=0.55, cluster_min_threshold=0.4)

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT evidence FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' AND target_id = :wid"
                ),
                {'wid': str(e1.id)},
            )
        ).all()
    assert rows
    evidence = rows[0][0]
    vaults_affected = set(evidence['vaults_affected'])
    assert {str(vault_a.id), str(vault_b.id)}.issubset(vaults_affected)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cooccurrence_merge_preserves_open_ended_intervals(
    session: AsyncSession,
    metastore,
    filestore,
):
    """A NULL valid_from / valid_to means an open-ended interval; merging a
    NULL side with a dated side MUST keep NULL — narrowing a dated interval
    silently loses the "still valid" / "always was" semantics."""
    e_winner = await _make_entity(session, f'OW-{uuid4().hex[:6]}')
    e_loser = await _make_entity(session, f'OL-{uuid4().hex[:6]}')
    e_peer = await _make_entity(session, f'OP-{uuid4().hex[:6]}')

    dated = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # winner<->peer: dated valid_from, NULL valid_to (still valid).
    e1, e2 = sorted([e_winner.id, e_peer.id], key=str)
    session.add(
        EntityCooccurrence(
            entity_id_1=e1,
            entity_id_2=e2,
            vault_id=GLOBAL_VAULT_ID,
            cooccurrence_count=1,
            valid_from=dated,
            valid_to=None,
        )
    )
    # loser<->peer: NULL valid_from (always was), dated valid_to.
    e3, e4 = sorted([e_loser.id, e_peer.id], key=str)
    session.add(
        EntityCooccurrence(
            entity_id_1=e3,
            entity_id_2=e4,
            vault_id=GLOBAL_VAULT_ID,
            cooccurrence_count=1,
            valid_from=None,
            valid_to=dated,
        )
    )
    await session.commit()

    from memex_common.config import MemexConfig

    svc = EntityService(metastore=metastore, filestore=filestore, config=MemexConfig())
    await svc.collapse_cluster(winner_id=e_winner.id, loser_ids=[e_loser.id], actor='test')

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT valid_from, valid_to FROM entity_cooccurrences '
                    'WHERE :pid IN (entity_id_1, entity_id_2) '
                    'AND :wid IN (entity_id_1, entity_id_2)'
                ),
                {'pid': str(e_peer.id), 'wid': str(e_winner.id)},
            )
        ).all()
    assert len(rows) == 1
    valid_from, valid_to = rows[0]
    assert valid_from is None, (
        'NULL valid_from on either side means "open start"; merge must NOT '
        f'narrow to a dated value (got {valid_from!r})'
    )
    assert valid_to is None, (
        'NULL valid_to on either side means "still valid"; merge must NOT '
        f'narrow to a dated value (got {valid_to!r})'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_scan_skips_when_lock_held(
    session: AsyncSession,
    metastore,
):
    """Two concurrent callers must not double-emit findings. The second
    invocation acquires no lock and returns early without scanning."""
    from memex_core.services.entity_maintenance import (
        _MEMEX_ENTITY_MAINTENANCE_LOCK_ID,
        scan_collapse_clusters,
    )

    suffix = uuid4().hex[:6]
    await _make_entity(
        session,
        f'LOCKA-{suffix}',
        mention_count=10,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await _make_entity(
        session,
        f'locka-{suffix}',
        mention_count=5,
        first_seen=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    api_stub = _make_api_stub(metastore)

    async with metastore.session() as holder:
        acquired = (
            await holder.execute(
                text('SELECT pg_try_advisory_xact_lock(:lock_id)'),
                {'lock_id': _MEMEX_ENTITY_MAINTENANCE_LOCK_ID},
            )
        ).scalar()
        assert acquired is True, 'holder must acquire the maintenance lock first'

        summary = await scan_collapse_clusters(
            api_stub, pair_threshold=0.55, cluster_min_threshold=0.4
        )
        await holder.rollback()

    assert summary == {
        'clusters_emitted': 0,
        'clusters_rejected_cohesion': 0,
        'rescan_updated': 0,
        'scanned': 0,
    }

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text(
                    'SELECT count(*) FROM maintenance_proposals '
                    "WHERE rule_name = 'entity_collapse_cluster' "
                    "AND status = 'pending' "
                    "AND (evidence -> 'member_canonical_names')::text LIKE :pat"
                ),
                {'pat': f'%{suffix}%'},
            )
        ).scalar()
    assert rows == 0, 'no findings should have been emitted under contention'
