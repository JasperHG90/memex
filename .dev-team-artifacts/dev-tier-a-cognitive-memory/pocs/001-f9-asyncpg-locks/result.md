# POC-001: F9 asyncpg advisory-lock semantics

| Field | Value |
|---|---|
| **Status** | PASS — all 5 scenarios green |
| **Author** | dev-ws-locks |
| **Date** | 2026-04-30 |
| **RFC** | RFC-005 (`/.dev-team-artifacts/dev-tier-a-cognitive-memory/rfcs/005-F9-advisory-locks.md`) |
| **Gates** | AC-F9-1 (lock-id non-collision substrate); AC-F9-6 (multi-worker concurrency) |
| **Effort** | ~3h (1 day budget; came in well under) |
| **Pre-POC certainty** | 88% (per RFC-005) |
| **Post-POC certainty** | 95% — unblocks ticket #23 (F9 ship) |

## Goal

Validate the load-bearing claims of RFC-005's connection-per-acquire design before committing to it across the F9 ship:

1. `pg_try_advisory_lock` / `pg_advisory_unlock` work correctly on a dedicated `asyncpg.connect(dsn)` connection.
2. The advisory lock SURVIVES SQLAlchemy session lifecycle (the dedicated asyncpg connection is OUTSIDE the SA pool).
3. Same-entity contention serializes (per AC-F9-6).
4. Different-entity acquires run truly concurrently (per AC-F9-6).
5. Connection death (process crash) auto-releases the lock (Postgres session-end semantics).

If any scenario failed → fall back to Python `asyncio.Lock` per entity_id (per RFC-005:268).

## Setup

- Postgres: `pgvector/pgvector:pg18-trixie` via `testcontainers.PostgresContainer`. Single session-scoped container shared across all 5 scenarios.
- Drivers: `asyncpg` for the dedicated lock connection; `sqlalchemy.ext.asyncio` for the SA-pool side of TC-12-2.
- Multiprocess scenarios (TC-12-3/4/5): `multiprocessing.get_context('spawn')` per TS guidance — fork shares pytest_asyncio's event loop and creates undefined behavior; spawn workers re-init Python and asyncio cleanly. Worker coordination uses `Manager().dict()` for monotonic-clock timestamps; explicitly NOT `multiprocessing.Event` (flakes on Docker spike).
- Lock-id helper: candidate `entity_lock_id(uuid)` per RFC-005:37-50 — `(1 << 62) | (int.from_bytes(uuid.bytes, 'big', signed=False) & ((1 << 62) - 1))`.
- pg_locks query semantics: empirically verified that single-int advisory locks are stored as `classid=high32, objid=low32, objsubid=1` (NOT the doc-implied `objid=low32, objsubid=high32`). Documented in `_helpers.py::split_lock_id_for_pg_locks`.

## Test command

```bash
cd /home/vscode/workspace/.claude/worktrees/dev-ws-locks
UV_PROJECT_ENVIRONMENT=.venv-dev-ws-locks uv run --active pytest \
    /home/vscode/workspace/.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/001-f9-asyncpg-locks/ -v
```

## Scenarios

| # | Description | Result | Evidence |
|---|---|---|---|
| TC-12-1 | Basic acquire/release semantics; pg_locks scoped by lock_id (TS refinement #1) | PASS | `pg_try_advisory_lock` returns `True`; pg_locks shows exactly 1 row with `granted=True`; `pg_advisory_unlock` returns `True`; pg_locks empty after release. |
| TC-12-2 | Lock survives SQLAlchemy AsyncSession lifecycle (RFC-005's load-bearing claim) | PASS | Lock acquired on dedicated asyncpg conn; SA AsyncSession opened+closed (engine disposed); pg_locks STILL shows the lock granted, pid matches the holder backend. |
| TC-12-3 | Same-entity contention serializes (AC-F9-6 half-1) | PASS | Worker A acquired at t₀, released at t₀+1.0s. Worker B started polling at t₀+0.1s, acquired at t≥A.releasing_at (Postgres did NOT grant B until A released). |
| TC-12-4 | Different-entity parallelism (AC-F9-6 half-2) | PASS | Worker A acquired lock_id_a, Worker B acquired lock_id_b within <500ms of each other; held intervals overlapped (`a_held ∩ b_held > 0` ns) — true parallelism. |
| TC-12-5 | Connection death auto-releases lock (RFC-005:264) | PASS | Crashing worker called `os._exit(1)` while holding lock (NO `conn.close()`, NO finally, NO atexit per QA caveat); subsequent worker acquired within `2s` of crash. Crasher exited with code 1 (verified). |

**Total runtime**: 5.76s on first run, 6.25s on the timestamped re-run at `2026-04-30T17:14:15Z` — both single-session, both 5/5 PASS, zero flakes observed across two clean back-to-back runs.

## Key findings

1. **RFC-005's connection-per-acquire model is sound.** TC-12-2 explicitly proved the dedicated asyncpg connection is fully isolated from the SA pool — no spurious release on session close.

2. **Spawn-only multiprocess.** TS was right to mandate `mp.get_context('spawn')`. An earlier draft of mine using fork would have inherited pytest_asyncio's event loop and likely deadlocked.

3. **`pg_locks` column semantics matter.** RFC-005 was correct that the lock_id is split into 32-bit halves, but the **column placement** (`classid` vs `objid` for high vs low) is the opposite of what one might expect. Documented in `_helpers.py` so #23's `services/locks.py` doesn't relearn this.

4. **Crash recovery is fast.** Postgres released the dead session's advisory lock within ~50ms in practice (well under the 2s bound). Adequate for production reconsolidate timeout (RFC-005's 30s default has 600x headroom).

5. **No new dependencies.** All scenarios used drivers already in the project venv (asyncpg, sqlalchemy, testcontainers, pytest-asyncio). Zero `pyproject.toml` churn — POC was drift-free as predicted.

## Implementation notes for #23 ship

- **`services/locks.py` should reuse the `entity_lock_id` formulation in `_helpers.py`** verbatim (modulo refactoring into a proper module). Document the bit-mask non-collision invariant inline.
- **Lock-state observation in production tests** can use the `PG_LOCKS_QUERY` pattern from this POC — scoped by `(classid, objid, objsubid=1)` to avoid cross-test pollution.
- **`asyncio.run()` in multiprocess workers** is the cleanest pattern; each spawned worker is independent so they don't compete for an outer event loop.
- **`os._exit(1)` is the only true crash simulator.** `conn.terminate()` is graceful; `process.terminate()` (SIGTERM) gives Python a chance to run cleanup. Document this in the #23 integration tests as a comment so future maintainers don't "fix" it back to a graceful close.

## Deferrals (out of POC scope; flagged for #23)

Per RFC-005 + team-lead direction, these are #23 ship concerns:
- 1M random UUID lock-id collision sweep against `MEMEX_LEADER_LOCK_ID` — pure unit test (`tests/unit/test_lock_id_derivation.py::test_no_collision_with_leader_lock`).
- `EntityLockTimeoutError` semantics + `acquire_entity_lock(timeout_seconds=...)` behavior.
- Connection-pool size 4 tuning under heavyweight reconsolidation burst.
- Soak loop / repeated-acquire flake tightening — POC saw zero flakes locally; if CI flakes appear, #23 ship adds retry semantics.

### RFC-005 / TS-refinement-#1 column-order correction (carry into #23 ship)

TS refinement #1 (forwarded with the #12 GO message) described the `pg_locks`
filter as `objid = <low32>, objsubid = <high32>`. POC-verified the actual
layout for single-int advisory locks against `pgvector/pgvector:pg18-trixie`
is the opposite assignment:

- `classid` = high 32 bits of the int64 lock_id
- `objid`   = low 32 bits of the int64 lock_id
- `objsubid` = `1` (single-int advisory lock marker; `2` indicates a two-int lock)

Reproducer:

```sql
SELECT pg_try_advisory_lock(x'40000000deadbeef'::bigint);
SELECT classid, objid, objsubid FROM pg_locks WHERE locktype = 'advisory';
-- classid  = 1073741824    (0x40000000  = high 32)
-- objid    = 3735928559    (0xDEADBEEF  = low 32)
-- objsubid = 1
```

RFC-005 itself does not contain `pg_locks` column-placement guidance, so no
in-place RFC edit is required. When #23 ships `services/locks.py` and its
integration tests, **copy the `split_lock_id_for_pg_locks()` +
`PG_LOCKS_QUERY` patterns from this POC's `_helpers.py:26-48`** rather than
re-deriving from the original TS-refinement wording.

## Conclusion

**PASS — RFC-005's design is validated.** `services/locks.py` may proceed in #23 with the dedicated-asyncpg-connection approach. No fallback to Python `asyncio.Lock` needed.
