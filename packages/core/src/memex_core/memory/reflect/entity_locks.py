"""Process-local asyncio locks keyed by entity_id for reflection serialization.

Intra-worker dedup: two coroutines reflecting on the same entity within one
worker process serialize on the same lock. Cross-worker dedup is handled by
the reflection queue's `SELECT ... FOR UPDATE SKIP LOCKED` claim — locks
here do not need to be visible to other processes.

Locks are created lazily on first acquisition and stored in a
``WeakValueDictionary`` so they can be garbage-collected once no coroutine
holds or is waiting on them — preventing unbounded growth of the registry
when many distinct entities are reflected on over time.

A creation lock guards the lazy-init path so two concurrent acquirers on a
not-yet-seen entity_id observe the same ``asyncio.Lock`` instance instead
of racing to create two siblings.
"""

from __future__ import annotations

import asyncio
import weakref
from uuid import UUID

_entity_locks: weakref.WeakValueDictionary[UUID, asyncio.Lock] = weakref.WeakValueDictionary()
_registry_lock = asyncio.Lock()


async def get_entity_lock(entity_id: UUID) -> asyncio.Lock:
    """Return the asyncio.Lock for ``entity_id``, creating it lazily.

    The returned lock is the canonical one for the entity within this process
    for the lifetime of any caller holding a reference to it. Once all
    references go out of scope the weak registry drops it; a subsequent call
    creates a fresh lock for that entity.
    """
    lock = _entity_locks.get(entity_id)
    if lock is not None:
        return lock
    async with _registry_lock:
        lock = _entity_locks.get(entity_id)
        if lock is None:
            lock = asyncio.Lock()
            _entity_locks[entity_id] = lock
        return lock


def _registry_size_for_tests() -> int:
    """Return current registry size — test-only helper."""
    return len(_entity_locks)
