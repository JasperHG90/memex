# ADR-001: Per-Entity Advisory Lock IDs Use a Disjoint High-Bit Range

## Status

Accepted

## Context

F9 introduces per-entity advisory locks so that concurrent reflection workers can serialize on a single entity without blocking the global scheduler. PostgreSQL `pg_advisory_lock` takes a single `bigint` (signed 64-bit), and the existing leader-election lock at `5432789123456789` already occupies that namespace.

A naive hash of `entity_id` (UUID) into `bigint` risks colliding with the leader lock, which would cause a worker to silently block the scheduler — or, worse, allow a worker to acquire the leader lock by accident and corrupt the leader-election protocol. We need a derivation that is provably disjoint from the leader value, cheap to compute, and collision-resistant across the realistic UUID population (millions of entities per vault).

## Decision

Per-entity advisory lock IDs are derived as:

```
(1 << 62) | (int.from_bytes(uuid.bytes, 'big', signed=False) & ((1 << 62) - 1))
```

The leader-election lock remains pinned at `5432789123456789`, whose bit 62 is unset. By construction, every per-entity lock has bit 62 set, and the leader does not — the two ranges are disjoint by inspection (`LEADER & (1 << 62) == 0`). A 1M-UUID collision test pins the implementation against accidental regressions.

Implemented in `packages/core/src/memex_core/services/locks.py`. Specified in RFC-005 and refined in RFC-008.

## Consequences

**Positive:**
- Disjointness is a single-line proof, not a probabilistic argument.
- 62-bit entropy from the UUID gives ample collision headroom.
- No coordination table needed — derivation is pure.
- Future namespaces can claim other high bits (bit 61, 60) using the same pattern.

**Negative:**
- Lock IDs are negative when interpreted as signed `bigint` (Postgres convention) — debugging output looks unusual.
- The convention is a hard contract: any future code that assigns advisory lock IDs in this codebase MUST consult this ADR before picking a range.

## Alternatives Considered

- **Plain `hash(uuid) % bigint_max`** — rejected: non-zero leader-collision probability, no proof of disjointness.
- **Two-key advisory locks (`pg_advisory_lock(int, int)`)** — rejected: would diverge from the existing single-key leader lock and require migrating that surface too.
- **Dedicated `entity_locks` table with row-level locks** — rejected: adds a write per acquisition and a contention point on the table itself; advisory locks are free and hold for the session.
