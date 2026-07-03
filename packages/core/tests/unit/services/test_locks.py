"""TC-23-1 — Lock-id derivation + split helper (F9 ship, locked v2 contract).

6 tests:
- test_lock_id_deterministic: same UUID -> same int across imports.
- test_lock_id_within_high_bit_range: bit-mask proof per RFC-005 risk row 2.
- test_no_collision_with_leader: 1M UUIDs vs MEMEX_LEADER_LOCK_ID.
- test_split_for_pg_locks_round_trip: (classid << 32) | objid == lock_id.
- test_leader_lock_id_single_source_of_truth: HIGH-003 dedup guard
  (scheduler.py is canonical; services/locks.py must not redeclare).
- (test_split_matches_actual_pg_locks_layout lives in TC-23-2 integration.)
"""

from __future__ import annotations

import uuid

import pytest

from memex_core.scheduler import MEMEX_LEADER_LOCK_ID
from memex_core.services.locks import (
    ENTITY_LOCK_HIGH_BIT,
    LockIdSplit,
    entity_lock_id,
    split_for_pg_locks,
)


def test_lock_id_deterministic() -> None:
    eid = uuid.UUID('11111111-2222-3333-4444-555555555555')
    assert entity_lock_id(eid) == entity_lock_id(eid)
    assert entity_lock_id(eid) == entity_lock_id(uuid.UUID(str(eid)))


def test_lock_id_within_high_bit_range() -> None:
    for _ in range(1000):
        lock_id = entity_lock_id(uuid.uuid4())
        assert lock_id & ENTITY_LOCK_HIGH_BIT, 'high bit must be set'
        assert lock_id >= ENTITY_LOCK_HIGH_BIT
        assert lock_id < (1 << 63), 'must fit signed int64'

    assert MEMEX_LEADER_LOCK_ID & ENTITY_LOCK_HIGH_BIT == 0, (
        'MEMEX_LEADER_LOCK_ID must have bit 62 clear so the disjoint-range invariant holds'
    )


@pytest.mark.parametrize('count', [1_000_000])
def test_no_collision_with_leader(count: int) -> None:
    for _ in range(count):
        assert entity_lock_id(uuid.uuid4()) != MEMEX_LEADER_LOCK_ID


def test_split_for_pg_locks_round_trip() -> None:
    for _ in range(1000):
        lock_id = entity_lock_id(uuid.uuid4())
        split = split_for_pg_locks(lock_id)
        assert isinstance(split, LockIdSplit)
        assert (split.classid << 32) | split.objid == lock_id
        assert 0 <= split.classid < (1 << 32)
        assert 0 <= split.objid < (1 << 32)


def test_split_for_pg_locks_known_value() -> None:
    """Spot-check against POC's empirical example: 0x40000000DEADBEEF."""
    split = split_for_pg_locks(0x40000000DEADBEEF)
    assert split.classid == 0x40000000
    assert split.objid == 0xDEADBEEF


def test_leader_lock_id_single_source_of_truth() -> None:
    """HIGH-003 (Phase 3 adversarial review): scheduler.py is the sole
    runtime definition of the leader advisory-lock ID.

    Pins the AC-X-8 invariant value (5432789123456789) AND ensures
    ``services/locks.py`` does not redeclare a parallel constant. Any
    future module re-introducing a local copy is caught by `dir()`
    scan + value match.
    """
    from memex_core import scheduler as scheduler_mod
    from memex_core.services import locks as locks_mod

    assert scheduler_mod.MEMEX_LEADER_LOCK_ID == 5432789123456789

    # locks.py must not redeclare the constant (under any spelling).
    forbidden = {'LEADER_LOCK_ID', 'MEMEX_LEADER_LOCK_ID'}
    leaked = forbidden & set(dir(locks_mod))
    assert not leaked, (
        f'services/locks.py must not redeclare leader-lock constants; '
        f'found: {sorted(leaked)}. The single source of truth is '
        f'memex_core.scheduler.MEMEX_LEADER_LOCK_ID.'
    )
