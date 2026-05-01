"""TC-23-1 — Lock-id derivation + split helper (F9 ship, locked v2 contract).

5 tests:
- test_lock_id_deterministic: same UUID -> same int across imports.
- test_lock_id_within_high_bit_range: bit-mask proof per RFC-005 risk row 2.
- test_no_collision_with_leader: 1M UUIDs vs MEMEX_LEADER_LOCK_ID.
- test_split_for_pg_locks_round_trip: (classid << 32) | objid == lock_id.
- (test_split_matches_actual_pg_locks_layout lives in TC-23-2 integration.)
"""

from __future__ import annotations

import uuid

import pytest

from memex_core.services.locks import (
    ENTITY_LOCK_HIGH_BIT,
    LEADER_LOCK_ID,
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

    assert LEADER_LOCK_ID & ENTITY_LOCK_HIGH_BIT == 0, (
        'LEADER_LOCK_ID must have bit 62 clear so the disjoint-range invariant holds'
    )


@pytest.mark.parametrize('count', [1_000_000])
def test_no_collision_with_leader(count: int) -> None:
    for _ in range(count):
        assert entity_lock_id(uuid.uuid4()) != LEADER_LOCK_ID


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
