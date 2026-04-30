"""POC-001 helpers — candidate F9 lock-id derivation + pg_locks query.

Not a production module. The real `services/locks.py` lands in #23 ship.
This file just makes the POC scenarios cleaner to read.
"""

from __future__ import annotations

import uuid


ENTITY_LOCK_HIGH_BIT = 1 << 62
ENTITY_LOCK_MASK = (1 << 62) - 1


def entity_lock_id(entity_id: uuid.UUID) -> int:
    """Derive a Postgres advisory lock id for an entity.

    Per RFC-005: high bit set so result is in [2^62, 2^63 - 1], disjoint from
    MEMEX_LEADER_LOCK_ID (~2^52).
    """
    raw = int.from_bytes(entity_id.bytes, 'big', signed=False) & ENTITY_LOCK_MASK
    return ENTITY_LOCK_HIGH_BIT | raw


def split_lock_id_for_pg_locks(lock_id: int) -> tuple[int, int]:
    """Split an int64 single-int advisory lock_id into (classid, objid) — the
    columns Postgres uses to expose it in pg_locks.

    Empirically verified against pgvector/pgvector:pg18-trixie:
        SELECT pg_try_advisory_lock(0x40000000deadbeef::bigint);
        SELECT classid, objid, objsubid FROM pg_locks WHERE locktype='advisory';
        --> classid=0x40000000 (high32), objid=0xDEADBEEF (low32), objsubid=1
    classid + objid are oid columns (asyncpg returns Python int directly).
    objsubid=1 marks "single-int advisory lock"; objsubid=2 would mark a
    two-int lock — not what we use.
    """
    high32 = (lock_id >> 32) & 0xFFFFFFFF
    low32 = lock_id & 0xFFFFFFFF
    return high32, low32


# Single-int advisory locks: classid=high32, objid=low32, objsubid=1.
PG_LOCKS_QUERY = (
    'SELECT pid, granted FROM pg_locks '
    "WHERE locktype = 'advisory' "
    'AND classid = $1 AND objid = $2 AND objsubid = 1'
)
