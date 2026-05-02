"""F49 — memex_get_unit_history graph-walk timeline integration tests.

Tests the contradiction-graph backward walk through `UnitsService.get_unit_history`.
Real Postgres via testcontainers, no LLM — we seed `MemoryLink` rows directly
to control the graph topology under test.

Link convention (verified at
``packages/core/src/memex_core/memory/contradiction/engine.py:225-227``):
``MemoryLink(from_unit_id=authoritative, to_unit_id=superseded)``. Walking
backward in time queries ``WHERE from_unit_id = current`` and collects
``to_unit_id`` values as predecessors.

Test cases:
1. Linear chain (A contradicts B contradicts C) — walk from A returns A→B→C in order.
2. DAG with shared predecessor (A weakened by B and C, both weakened by D) — D
   appears exactly once across the tree (visited-set guard).
3. Literal cycle (A weakens B, B weakens A) — synthetic fixture; walk
   terminates at max_depth without recursion overflow.
4. Orphan starting unit (no contradiction links) — root only, no predecessors.
5. Vault scoping — links in vault A do not surface vault B's links.
6. ``reinforces`` excluded — default walk skips ``reinforces`` even when present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.exceptions import MemoryUnitNotFoundError
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryLink, MemoryUnit, Note, Vault
from memex_core.services.units import UnitsService

pytestmark = [pytest.mark.integration]


def _make_note(vault_id: UUID, suffix: str = '') -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title=f'Test Note {suffix}',
        content_hash=str(uuid4()),
        original_text=f'content {uuid4()}',
    )


def _make_unit(
    note_id: UUID,
    vault_id: UUID,
    text: str,
    *,
    confidence: float = 1.0,
    event_date: datetime | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uuid4(),
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=confidence,
        event_date=event_date or datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )


def _make_link(
    *,
    authoritative: UUID,
    superseded: UUID,
    vault_id: UUID,
    link_type: str = 'contradicts',
    reasoning: str = 'test',
) -> MemoryLink:
    return MemoryLink(
        from_unit_id=authoritative,
        to_unit_id=superseded,
        link_type=link_type,
        vault_id=vault_id,
        weight=1.0,
        link_metadata={
            'authoritative_unit_id': str(authoritative),
            'superseded_unit_id': str(superseded),
            'reasoning': reasoning,
            'temporal_basis': 'timestamp',
        },
    )


@pytest.fixture
def units_service(metastore, filestore, memex_config) -> UnitsService:
    return UnitsService(metastore=metastore, filestore=filestore, config=memex_config)


async def test_linear_contradiction_chain_walks_in_order(units_service: UnitsService, session):
    """Linear A -> B -> C chain: A contradicts B, B contradicts C.

    Walk from A returns root=A (depth=0) with one predecessor=B (depth=1)
    that itself has one predecessor=C (depth=2).
    """
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'linear')
    unit_c = _make_unit(note.id, vault_id, 'oldest claim C', event_date=now - timedelta(days=10))
    unit_b = _make_unit(note.id, vault_id, 'middle claim B', event_date=now - timedelta(days=5))
    unit_a = _make_unit(note.id, vault_id, 'newest claim A', event_date=now)

    session.add(note)
    for unit in (unit_c, unit_b, unit_a):
        session.add(unit)
    await session.flush()

    session.add(
        _make_link(
            authoritative=unit_a.id,
            superseded=unit_b.id,
            vault_id=vault_id,
            link_type='contradicts',
            reasoning='A overrides B',
        )
    )
    session.add(
        _make_link(
            authoritative=unit_b.id,
            superseded=unit_c.id,
            vault_id=vault_id,
            link_type='contradicts',
            reasoning='B overrides C',
        )
    )
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id)

    assert history.unit_id == unit_a.id
    assert history.depth == 0
    assert history.link_type is None
    assert len(history.predecessors) == 1

    node_b = history.predecessors[0]
    assert node_b.unit_id == unit_b.id
    assert node_b.depth == 1
    assert node_b.link_type == 'contradicts'
    assert node_b.link_metadata.get('reasoning') == 'A overrides B'
    assert len(node_b.predecessors) == 1

    node_c = node_b.predecessors[0]
    assert node_c.unit_id == unit_c.id
    assert node_c.depth == 2
    assert node_c.link_type == 'contradicts'
    assert node_c.predecessors == []
    assert node_c.truncated is False


async def test_dag_with_shared_predecessor_visits_each_node_once(
    units_service: UnitsService, session
):
    """DAG branching: A weakened by B and C, both B and C weakened by D.

    Walk from A returns a tree where D appears under exactly one branch
    (visited-set guard prevents the second visit). The other branch reports
    truncated=True at its leaf node, signalling there's a predecessor that
    was already visited.
    """
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'dag')
    unit_d = _make_unit(note.id, vault_id, 'oldest D', event_date=now - timedelta(days=20))
    unit_b = _make_unit(note.id, vault_id, 'mid B', event_date=now - timedelta(days=10))
    unit_c = _make_unit(note.id, vault_id, 'mid C', event_date=now - timedelta(days=8))
    unit_a = _make_unit(note.id, vault_id, 'newest A', event_date=now)
    session.add(note)
    for u in (unit_d, unit_b, unit_c, unit_a):
        session.add(u)
    await session.flush()

    session.add(
        _make_link(
            authoritative=unit_a.id, superseded=unit_b.id, vault_id=vault_id, link_type='weakens'
        )
    )
    session.add(
        _make_link(
            authoritative=unit_a.id, superseded=unit_c.id, vault_id=vault_id, link_type='weakens'
        )
    )
    session.add(
        _make_link(
            authoritative=unit_b.id, superseded=unit_d.id, vault_id=vault_id, link_type='weakens'
        )
    )
    session.add(
        _make_link(
            authoritative=unit_c.id, superseded=unit_d.id, vault_id=vault_id, link_type='weakens'
        )
    )
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id)

    def _collect(node) -> list[UUID]:
        out = [node.unit_id]
        for c in node.predecessors:
            out.extend(_collect(c))
        return out

    visited_ids = _collect(history)
    assert visited_ids.count(unit_d.id) == 1, (
        f'D must appear exactly once across the tree, got {visited_ids.count(unit_d.id)}'
    )
    assert set(visited_ids) == {unit_a.id, unit_b.id, unit_c.id, unit_d.id}

    branches_with_d = [
        p for p in history.predecessors if any(g.unit_id == unit_d.id for g in p.predecessors)
    ]
    branches_without_d = [
        p for p in history.predecessors if not any(g.unit_id == unit_d.id for g in p.predecessors)
    ]
    assert len(branches_with_d) == 1
    assert len(branches_without_d) == 1

    pruned_leaf = branches_without_d[0]
    assert pruned_leaf.predecessors == []
    assert pruned_leaf.truncated is True


async def test_literal_cycle_terminates_at_max_depth(units_service: UnitsService, session):
    """Synthetic literal cycle: A weakens B, B weakens A.

    The contradiction engine wouldn't normally create this, but the walk
    must defensively terminate. The visited-set guard short-circuits the
    cycle on the first re-encounter; max_depth is the second line of defense.
    """
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'cycle')
    unit_a = _make_unit(note.id, vault_id, 'A', event_date=now)
    unit_b = _make_unit(note.id, vault_id, 'B', event_date=now - timedelta(days=1))
    session.add(note)
    for u in (unit_a, unit_b):
        session.add(u)
    await session.flush()

    session.add(
        _make_link(
            authoritative=unit_a.id, superseded=unit_b.id, vault_id=vault_id, link_type='weakens'
        )
    )
    session.add(
        _make_link(
            authoritative=unit_b.id, superseded=unit_a.id, vault_id=vault_id, link_type='weakens'
        )
    )
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id, max_depth=5)

    assert history.unit_id == unit_a.id
    assert history.depth == 0
    assert len(history.predecessors) == 1
    node_b = history.predecessors[0]
    assert node_b.unit_id == unit_b.id
    assert node_b.depth == 1
    assert node_b.predecessors == []
    assert node_b.truncated is True


async def test_max_depth_zero_returns_root_only(units_service: UnitsService, session):
    """max_depth=0: return root only, even when predecessors exist."""
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'depth0')
    unit_a = _make_unit(note.id, vault_id, 'A', event_date=now)
    unit_b = _make_unit(note.id, vault_id, 'B', event_date=now - timedelta(days=1))
    session.add(note)
    for u in (unit_a, unit_b):
        session.add(u)
    await session.flush()
    session.add(_make_link(authoritative=unit_a.id, superseded=unit_b.id, vault_id=vault_id))
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id, max_depth=0)

    assert history.unit_id == unit_a.id
    assert history.predecessors == []
    assert history.truncated is True


async def test_orphan_starting_unit_returns_root_only(units_service: UnitsService, session):
    """Unit with no contradiction links: returns root, no predecessors, not truncated."""
    vault_id = GLOBAL_VAULT_ID
    note = _make_note(vault_id, 'orphan')
    unit_a = _make_unit(note.id, vault_id, 'standalone')
    session.add(note)
    session.add(unit_a)
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id)

    assert history.unit_id == unit_a.id
    assert history.depth == 0
    assert history.link_type is None
    assert history.predecessors == []
    assert history.truncated is False


async def test_vault_scoping_isolates_chains(units_service: UnitsService, session):
    """Two vaults each with their own contradiction chain.

    Walk from a unit in vault A does NOT surface links from vault B, even if
    a hostile MemoryLink row exists pointing across vaults (which would be a
    P0 leak — guarded by the vault filter on every link query).
    """
    vault_a = GLOBAL_VAULT_ID
    vault_b_id = uuid4()
    session.add(Vault(id=vault_b_id, name='other-vault'))
    await session.flush()
    now = datetime.now(timezone.utc)

    note_a = _make_note(vault_a, 'A')
    note_b = _make_note(vault_b_id, 'B')
    unit_a_old = _make_unit(note_a.id, vault_a, 'A old', event_date=now - timedelta(days=5))
    unit_a_new = _make_unit(note_a.id, vault_a, 'A new', event_date=now)
    unit_b_old = _make_unit(note_b.id, vault_b_id, 'B old', event_date=now - timedelta(days=5))
    unit_b_new = _make_unit(note_b.id, vault_b_id, 'B new', event_date=now)
    session.add(note_a)
    session.add(note_b)
    for u in (unit_a_old, unit_a_new, unit_b_old, unit_b_new):
        session.add(u)
    await session.flush()

    session.add(_make_link(authoritative=unit_a_new.id, superseded=unit_a_old.id, vault_id=vault_a))
    session.add(
        _make_link(authoritative=unit_b_new.id, superseded=unit_b_old.id, vault_id=vault_b_id)
    )
    await session.commit()

    history_a = await units_service.get_unit_history(unit_a_new.id, vault_id=vault_a)

    a_ids = {history_a.unit_id} | {p.unit_id for p in history_a.predecessors}
    assert unit_b_new.id not in a_ids
    assert unit_b_old.id not in a_ids
    assert {unit_a_new.id, unit_a_old.id} == a_ids

    history_b = await units_service.get_unit_history(unit_b_new.id, vault_id=vault_b_id)
    b_ids = {history_b.unit_id} | {p.unit_id for p in history_b.predecessors}
    assert unit_a_new.id not in b_ids
    assert unit_a_old.id not in b_ids


async def test_cross_vault_unit_id_raises_not_found(units_service: UnitsService, session):
    """Asking for a unit in vault A while supplying vault B raises MemoryUnitNotFoundError.

    This is the per-row scope check: the route layer's check_vault_access
    is the principal-vault gate; this is the backstop for in-scope keys
    that supply a mismatched (unit_id, vault_id) pair.
    """
    vault_b_id = uuid4()
    session.add(Vault(id=vault_b_id, name='other-vault-2'))
    await session.flush()

    note_a = _make_note(GLOBAL_VAULT_ID, 'CrossA')
    unit_a = _make_unit(note_a.id, GLOBAL_VAULT_ID, 'A only')
    session.add(note_a)
    session.add(unit_a)
    await session.commit()

    with pytest.raises(MemoryUnitNotFoundError):
        await units_service.get_unit_history(unit_a.id, vault_id=vault_b_id)


async def test_reinforces_link_excluded_from_default_walk(units_service: UnitsService, session):
    """A unit with both `weakens` AND `reinforces` predecessors.

    Default walk excludes the `reinforces` predecessor (would point forward
    in time when followed backward — newer reinforcing older). Only the
    `weakens` predecessor surfaces.
    """
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'reinforces-test')
    unit_a = _make_unit(note.id, vault_id, 'authoritative A', event_date=now)
    unit_weak = _make_unit(note.id, vault_id, 'older weakened', event_date=now - timedelta(days=5))
    unit_reinforced = _make_unit(
        note.id, vault_id, 'older reinforced', event_date=now - timedelta(days=10)
    )
    session.add(note)
    for u in (unit_a, unit_weak, unit_reinforced):
        session.add(u)
    await session.flush()

    session.add(
        _make_link(
            authoritative=unit_a.id,
            superseded=unit_weak.id,
            vault_id=vault_id,
            link_type='weakens',
        )
    )
    session.add(
        _make_link(
            authoritative=unit_a.id,
            superseded=unit_reinforced.id,
            vault_id=vault_id,
            link_type='reinforces',
        )
    )
    await session.commit()

    history = await units_service.get_unit_history(unit_a.id, vault_id=vault_id)

    pred_ids = {p.unit_id for p in history.predecessors}
    assert unit_weak.id in pred_ids
    assert unit_reinforced.id not in pred_ids, (
        'reinforces link must NOT surface in the default backward walk'
    )
    assert len(history.predecessors) == 1


async def test_unknown_unit_id_raises_not_found(units_service: UnitsService, session):
    """Asking for a non-existent unit_id raises MemoryUnitNotFoundError."""
    with pytest.raises(MemoryUnitNotFoundError):
        await units_service.get_unit_history(uuid4(), vault_id=GLOBAL_VAULT_ID)


async def test_max_depth_negative_raises(units_service: UnitsService, session):
    """Negative max_depth is rejected."""
    note = _make_note(GLOBAL_VAULT_ID, 'neg')
    unit_a = _make_unit(note.id, GLOBAL_VAULT_ID, 'X')
    session.add(note)
    session.add(unit_a)
    await session.commit()

    with pytest.raises(ValueError):
        await units_service.get_unit_history(unit_a.id, vault_id=GLOBAL_VAULT_ID, max_depth=-1)


async def test_predecessors_sorted_oldest_first(units_service: UnitsService, session):
    """When a unit has multiple predecessors, they're returned oldest-first.

    Stable ordering matters for downstream consumers building timelines —
    branching predecessors should appear in temporal order, not insertion
    order.
    """
    vault_id = GLOBAL_VAULT_ID
    now = datetime.now(timezone.utc)
    note = _make_note(vault_id, 'order')
    unit_root = _make_unit(note.id, vault_id, 'root', event_date=now)
    unit_old = _make_unit(note.id, vault_id, 'oldest pred', event_date=now - timedelta(days=20))
    unit_mid = _make_unit(note.id, vault_id, 'middle pred', event_date=now - timedelta(days=10))
    unit_recent = _make_unit(note.id, vault_id, 'recent pred', event_date=now - timedelta(days=2))
    session.add(note)
    for u in (unit_root, unit_old, unit_mid, unit_recent):
        session.add(u)
    await session.flush()

    for pred in (unit_recent, unit_old, unit_mid):
        session.add(_make_link(authoritative=unit_root.id, superseded=pred.id, vault_id=vault_id))
    await session.commit()

    history = await units_service.get_unit_history(unit_root.id, vault_id=vault_id)

    pred_dates = [p.event_date for p in history.predecessors]
    assert pred_dates == sorted(pred_dates), (
        f'predecessors must be oldest-first by event_date, got {pred_dates}'
    )
    pred_ids = [p.unit_id for p in history.predecessors]
    assert pred_ids == [unit_old.id, unit_mid.id, unit_recent.id]
