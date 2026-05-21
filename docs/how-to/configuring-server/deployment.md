# Deploy Memex behind a connection pooler

You want to run Memex with several worker processes behind PgBouncer (or pgcat, or Odyssey). Multi-worker is safe out of the box — Memex elects one leader per cluster so background work only runs in one place. The pooler in front of it is not safe out of the box. This page walks you through the deployment, names the one configuration knob that ruins it, and shows you how to confirm the leader is the only worker draining the reflection queue.

> **Read this first.** Memex's leader election uses **session-scoped** Postgres advisory locks. PgBouncer's default `transaction` pool mode silently releases those locks at every `COMMIT`. Two workers will then both believe they are leader, the reflection drain double-fires, and the lint pass double-bands the same units. You MUST set PgBouncer to `pool_mode = session` (or the equivalent in pgcat / Odyssey) before you send live traffic through the pooler <code-ref path="packages/core/src/memex_core/scheduler.py" lines="610-640" />.

## Prerequisites

- A Postgres 14+ instance with the `vector` extension installed (the schema migration handles the rest).
- A PgBouncer, pgcat, or Odyssey instance you can configure.
- A Memex install with `memex-core[server]` and the schema migrated (`memex database upgrade`).
- A config file at a path the server can read — either the `.memex.yaml` in its working directory or the path in `MEMEX_CONFIG_PATH`.

## Procedure

### 1. Point Memex at the pooler

Memex reads its database connection from `server.meta_store.instance.connection_string`. Set it to the pooler's listening address, not the Postgres backend's:

```yaml
server:
  meta_store:
    instance:
      connection_string: "postgresql+asyncpg://memex:secret@pgbouncer.internal:6432/memex"
```

The `+asyncpg` driver suffix stays — the scheduler strips it internally before opening its leader-election connection <code-ref path="packages/core/src/memex_core/scheduler.py" lines="600-602" />.

### 2. Configure PgBouncer in `session` mode

In `pgbouncer.ini`:

```ini
[databases]
memex = host=postgres.internal port=5432 dbname=memex

[pgbouncer]
listen_port = 6432
pool_mode = session
max_client_conn = 200
default_pool_size = 25
```

`pool_mode = session` is non-negotiable. Two other modes look attractive and are not:

- `transaction` releases the connection at every `COMMIT`. Every advisory lock Memex holds — the global leader lock, the per-vault lint lock, the per-entity reconsolidation lock — vanishes with it.
- `statement` is even more aggressive and breaks the same way.

The leader lock is by construction session-scoped — it must outlive every transaction the leader runs, because the whole point is that *one* worker owns it for the lifetime of the process. There is no `pg_try_advisory_xact_lock` variant that would let you fix this from the application side <code-ref path="DESIGN_DOCUMENT.md" lines="846-857" />.

If you run pgcat or Odyssey, the equivalent setting is the same word — `pool_mode: session` in pgcat's `pools.<name>` block, `pool_mode session` in Odyssey's route config.

Reload PgBouncer and confirm:

```bash
psql -h pgbouncer.internal -p 6432 -U pgbouncer pgbouncer -c "SHOW DATABASES;"
```

Look for `memex` with `pool_mode = session`.

### 3. Start Memex with multiple workers

The CLI launches Granian in production mode with two workers by default; pass `--workers` to scale up:

```bash
memex server start --workers 4 --host 0.0.0.0 --port 8000
```

Or run uvicorn directly if you prefer:

```bash
uvicorn memex_core.server:app --host 0.0.0.0 --port 8000 --workers 4
```

Each worker process boots its own scheduler coroutine. They race for the leader lock; one wins, the rest sleep for 60 seconds and try again <code-ref path="packages/core/src/memex_core/scheduler.py" lines="604-645" />.

### 4. Verify exactly one worker took the leader role

Tail the application log right after start-up. You should see this line **once**, from one worker:

```
Scheduler: Lock acquired. I am LEADER. Starting AioClock...
```

Every other worker stays silent at that level — they reached the lock, failed to take it, closed the connection, and went to sleep. If you see the "I am LEADER" line from two workers in the same boot, leader election is broken; jump to Troubleshooting.

If you see this line instead, the scheduler is intentionally off:

```
Scheduler: Background reflection DISABLED.
```

Set `server.memory.reflection.background_reflection_enabled: true` (it defaults to `true` in code; explicit is clearer) and restart <code-ref path="packages/common/src/memex_common/config.py" lines="542-545" />.

### 5. Scale horizontally

Add more workers — or more host machines pointing at the same pooler — without coordinating. The leader lock survives across machines because it lives in Postgres. When the leader dies, its connection drops, Postgres releases the lock, and the next polling worker (each follower retries every 60 seconds) picks it up. Expect up to 60 seconds of background-work pause during fail-over; foreground HTTP traffic is unaffected.

## Verification

Three checks confirm the setup is working.

### One worker drains the reflection queue

Watch the log for two minutes. Periodic ticks log on every interval (default 600 seconds; lower it for the test):

```
Scheduler: Running periodic reflection check...
```

Only the leader emits this line. If you see it from more than one worker in the same window, the pooler is releasing the leader lock — confirm `pool_mode = session` on every pool in the path.

### Advisory locks are visible in Postgres

Connect to the **backend** Postgres directly (not through the pooler — its session mapping will hide which client owns which lock):

```bash
psql -h postgres.internal -p 5432 -U memex -d memex \
  -c "SELECT pid, locktype, classid, objid FROM pg_locks WHERE locktype = 'advisory';"
```

You should see exactly one row whose `(classid, objid)` decodes to `5432789123456789` — that is `MEMEX_LEADER_LOCK_ID`, the global leader lock <code-ref path="packages/core/src/memex_core/scheduler.py" lines="20" />. Per-vault lint locks appear and disappear as the lint pass runs; per-entity reconsolidation locks live in the `[2^62, 2^63-1]` band and are disjoint by construction from the leader lock <code-ref path="packages/core/src/memex_core/services/locks.py" lines="43-82" />.

### Only one worker runs the lint pass at a time

The lint pass holds a per-vault non-blocking advisory lock and pairs it with `FOR UPDATE SKIP LOCKED` on the candidate select, so even during a brief two-leader window (a fail-over) the same unit cannot be double-banded <code-ref path="packages/core/src/memex_core/scheduler.py" lines="280-327" />. To check: trigger a lint pass and grep the log for `Scheduler: Lint emitted` — you should see one line per vault per tick, not two.

The standard health probes confirm the worker itself is alive — `GET /api/v1/health` returns 200 as long as the process is running, and `GET /api/v1/ready` returns 200 only when the database and file store are both reachable <code-ref path="packages/core/src/memex_core/server/health.py" lines="19-60" />. Neither probe tells you which worker is leader; that information is in the log line above.

## Troubleshooting

### The lint pass double-fires

You see two workers both log `Scheduler: Lint emitted N findings in vault X` in the same tick, or you see units flipped to `is_deprioritized = true` twice in a row when you expect a single transition.

The cause is almost always the pooler. Check, in order:

1. `SHOW DATABASES` in PgBouncer — confirm `pool_mode = session` for the `memex` entry.
2. Restart PgBouncer after you change `pool_mode` — running PgBouncer does not pick up `pgbouncer.ini` changes without `RELOAD` or a restart.
3. Look for an intermediate pooler you forgot about — a sidecar pgcat, a haproxy doing TCP load-balancing across two PgBouncers with different settings, a managed-Postgres provider that splices in its own pooler.
4. As a temporary mitigation, point Memex directly at the Postgres backend (skip the pooler) and confirm the double-firing stops. If it does, the pooler is the cause; fix the mode and re-route.

### The reflection drain is not happening

You see `Scheduler: Running periodic reflection check...` from no worker. Check:

1. Is the scheduler off? Grep for `Scheduler: Background reflection DISABLED.` in the log. If yes, set `server.memory.reflection.background_reflection_enabled: true` (or `MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED=true`) and restart.
2. Is no worker leader? Grep for `Scheduler: Lock acquired. I am LEADER.` after the last boot. If you see no such line, the workers are all polling and failing. Check the database is reachable from every worker — `GET /api/v1/ready` should return 200 from each one.
3. Is the leader connection dropping? Look for `Scheduler: Lost Postgres connection! stepping down...` — this prints when the leader's dedicated asyncpg connection closes. Common causes: a pooler-imposed connection idle timeout, a network blip, a Postgres restart. The follower poll picks up the lock within 60 seconds; if the connection keeps churning, the leader role thrashes between workers and no tick completes a full cycle.

### Advisory-lock contention spikes

You see `EntityLockTimeoutError` raised on `memex_memory_reconsolidate` calls, or contention on `pg_advisory_lock` in `pg_locks`. Two causes:

1. **A legitimately contested entity.** Two callers asked to reconsolidate the same entity at once. The lock acquires with a bounded spin (default 30 seconds) and then surfaces the timeout — the caller retries. This is the expected behaviour <code-ref path="packages/core/src/memex_core/services/locks.py" lines="100-173" />.
2. **A stale lock owner.** A process crashed mid-hold and the connection has not yet been torn down. Postgres releases the lock when the backend terminates; force-close the offending session with `SELECT pg_terminate_backend(pid)` (look up the `pid` in `pg_locks`). If this happens repeatedly, the upstream process is crashing — investigate the worker logs.

## See also

- [How-to: Secure Memex with an API key](api-key.md)
- [Reference: Configuration](../../reference/configuration.md)
- [Reference: Observability](../../reference/observability.md)
- [Explanation: Architecture overview](../../explanation/architecture-overview.md)
