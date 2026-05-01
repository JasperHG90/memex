"""Per-entity Postgres advisory-lock helpers (F9).

Single-int advisory locks keyed off entity UUID. The high bit (1 << 62) places
all entity lock ids in [2^62, 2^63-1] — disjoint by construction from the
leader lock at ~2^52. See RFC-005.

Acquire/release pattern uses a dedicated short-lived asyncpg connection per
context-manager invocation. Lock release is deterministic on context exit; if
the connection dies mid-hold (process crash, network failure), Postgres
auto-releases the lock when the backend terminates. Both behaviours validated
in POC-F9 (commit bba8094, merged at 866ec74).
"""

from __future__ import annotations

import uuid
from typing import Final, NamedTuple

from memex_common.exceptions import MemexError

LEADER_LOCK_ID: Final[int] = 5432789123456789
ENTITY_LOCK_HIGH_BIT: Final[int] = 1 << 62
ENTITY_LOCK_MASK: Final[int] = (1 << 62) - 1


class EntityLockTimeoutError(MemexError):
    """Raised when a per-entity advisory lock could not be acquired in time.

    Indicates concurrent reconsolidation on the same entity. Surface to the
    user as 'another reconsolidation is in progress; retry in a moment'.
    """

    pass


class LockIdSplit(NamedTuple):
    """Pair of (classid, objid) shown in pg_locks for single-int advisory locks.

    objsubid is always 1 for single-int advisory locks (and 2 for two-int);
    not part of this split because it's a constant for our scheme.
    """

    classid: int
    objid: int


def entity_lock_id(entity_id: uuid.UUID) -> int:
    """Derive a deterministic int64 advisory lock id from an entity UUID.

    The high bit is set so the result lives in [2^62, 2^63-1], disjoint from
    LEADER_LOCK_ID (~2^52). Deterministic by construction — the same UUID
    yields the same lock_id across processes (no PYTHONHASHSEED dependency).
    """
    raw = int.from_bytes(entity_id.bytes, 'big', signed=False) & ENTITY_LOCK_MASK
    return ENTITY_LOCK_HIGH_BIT | raw


def split_for_pg_locks(lock_id: int) -> LockIdSplit:
    """Split a single-int advisory lock id into the (classid, objid) pair
    Postgres exposes in pg_locks.

    Empirically verified against pg18-trixie: classid is the high 32 bits,
    objid is the low 32 bits, objsubid is always 1 for single-int locks.
    """
    classid = (lock_id >> 32) & 0xFFFFFFFF
    objid = lock_id & 0xFFFFFFFF
    return LockIdSplit(classid=classid, objid=objid)
