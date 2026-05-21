# Failure modes

This reference covers what each Memex component does when a dependency breaks, an LLM call refuses, a lock contends, or a budget runs out. The contract is the same across the system: optional components fail open so the user keeps making progress; required components fail closed with a clear surface for retry. Look up a component in the index, then read its row's detail block for the failure trigger, behaviour, retry policy, and observable signal.

Two terms used throughout this page:

- **Fail-open** — the component reports the failure, returns whatever partial result it can, and lets the call complete. The user sees a degraded answer, not an error.
- **Blocks** — the component surfaces the failure to the caller as an exception or non-2xx response. The caller decides how to retry.

The behaviour cited here is verified against the code paths named in each entry's `<code-ref>`. When source line numbers drift, the path-level anchor remains valid; check the cited file.

## Index

| Component | Fail-open or blocks | Observable signal | Recovery |
|---|---|---|---|
| Cross-encoder reranker | Fail-open | `logger.debug('Reranking model unavailable, skipping: %s', e)`; `self.reranker is None` in `RetrievalEngine` | Auto-skipped per request; install ONNX backend or configure `LitellmRerankerBackend` to re-enable |
| LLM circuit breaker | Blocks per call once open | `CircuitBreakerOpen.time_until_reset`; state transitions logged at INFO/WARNING | Auto-probe in HALF_OPEN after `reset_timeout_seconds`; success returns to CLOSED |
| LLM extraction on a chunk | Fail-open across siblings | `logger.warning('%d/%d chunk(s) failed extraction, proceeding…')`; raises only when every chunk fails | Other chunks proceed; full failure raises `ExtractionError` to the caller |
| Embedding service | Blocks ingestion | Per-backend exception propagates (no swallow) | Ingestion call returns the error; reads not depending on new embeddings are unaffected |
| Reflection task LLM call | Retry up to `max_retries`, then dead-letter | Row status moves `PROCESSING → FAILED → DEAD_LETTER`; `last_error` populated | `memex memory dead-letter retry` (CLI) or queue-service replay |
| Phase 5 CAS abandon | Re-enqueues without retry-count bump | `last_error = 'CAS abandon (concurrent refresh won)'`; row stays at original `retry_count` | Next scheduler tick re-claims via `SKIP LOCKED`; benign concurrency outcome, not a failure |
| Per-entity Postgres advisory lock | Blocks the caller until timeout | `EntityLockTimeoutError.timeout_seconds`; `memex_entity_lock_acquires_total{outcome='timeout'}` | Caller retries; surface as HTTP 409 with `Retry-After` derived from `timeout_seconds` |
| Per-entity asyncio lock (intra-worker) | Serialises within the worker | Coroutines queue on the `asyncio.Lock`; weak-registry size visible via `_registry_size_for_tests` | Lock released automatically when the holding coroutine exits |
| Per-vault FSFM auto-band lock | Skip this tick | `memex_fsfm_auto_band_skipped_total{reason='lock_held'}`; `AutoDeprioritizeSummary.skipped_lock_held=True` | Next scheduled tick re-attempts |
| FSFM auto-band cooldown | Skip the unit | `memex_fsfm_auto_band_skipped_total{reason='cooldown_active'}`; `AutoDeprioritizeSummary.skipped_cooldown` populated | Unit becomes eligible again `cooldown_days` after the last `memory_restore` |
| Surprise-gated lint cost cap | Defer the unit to a queue | `MaintenanceProposal.rule_name='_RULE_LLM_DEFERRED'` with `evidence.reason='cost_cap_exceeded'` | Next tick drains the deferred queue when the rolling 24h bucket has headroom |
| Rule-only lint pass | Independent of the LLM gate | Pending `MaintenanceProposal` rows continue to be emitted regardless of LLM budget | No action required — rule pass keeps running |
| Consolidation orchestrator | Per-entity skip, tick continues | `consolidation.tick.entity_deferred` log; `ConsolidationTick.error` recorded; per-entity error logged but tick row written | Next tick reclaims deferred entities; partial progress is durable |
| Scheduler leader lock (DB connection lost) | Steps down | `Scheduler: Lost Postgres connection! stepping down...` (ERROR); leader role released | Another worker acquires the advisory lock on its next poll loop |
| FSFM SQL↔Python parity (test gate) | Build-time guard | `pytest -m integration test_int_fsfm_sql_python_parity` fails when SQL and Python composite scores diverge | Reconcile the SQL CTE in `services/lint.py` with `services/deprioritize_score.py` |

## Lock hierarchy

Three lock scopes coexist and never collide by construction:

- `MEMEX_LEADER_LOCK_ID` (a single int64 at `5432789123456789`) gates every periodic task in the scheduler. Only one worker holds it at a time.
- Per-entity advisory locks live in `[2^62, 2^63-1]` — derived from the entity UUID and disjoint from the leader lock by virtue of the high bit. Used by reconsolidate and the consolidation orchestrator.
- Per-vault FSFM auto-band locks use the two-arg `pg_(try_)advisory_xact_lock(int4, int4)` form with namespace `0x46_53_46_4D` ("FSFM" in ASCII), so they cannot collide with single-int locks.

A brief leader flap can therefore still acquire a per-entity or per-vault lock for the next vault tick without colliding with the previous leader's outstanding work.

## How retries compose

The system layers four retry surfaces and they do not stack arbitrarily.

- **Circuit breaker** short-circuits LLM calls when a provider is failing repeatedly. It runs before any other retry.
- **Reflection queue** retries up to `max_retries=3` per row before moving to `DEAD_LETTER`. Dead-lettered rows are kept for inspection.
- **CAS abandon** re-enqueues without bumping the retry count, because the abandon is benign concurrency rather than a fault.
- **Scheduler loop** re-acquires the leader lock on a fresh asyncpg connection whenever the prior holder steps down. Followers poll on a 60-second cadence.

A reflection task that hits an LLM error consumes one of its three retries; a task that loses a CAS race does not. A consolidation tick that defers an entity for lock-timeout does not consume any retry budget — the deferred entity is re-selected by the diff query on the next tick.

## Cross-encoder reranker

**Failure trigger.** The ONNX session cannot load (model file missing, weights corrupted, `onnxruntime` import fails) or the configured LiteLLM backend rejects the request.

**Behaviour.** `get_retrieval_engine` wraps the model load in `try/except (ImportError, ValueError, RuntimeError, OSError)` and sets `reranker = None` on failure. Every downstream call gates on `use_reranker = self.reranker is not None and request.rerank`, so retrieval still runs — it returns the RRF-fused candidate set without the cross-encoder boost. <code-ref path="packages/core/src/memex_core/memory/retrieval/engine.py" lines="327-335" />

**Retry policy.** No automatic retry. The next process restart re-attempts the model load. <code-ref path="packages/core/src/memex_core/memory/models/reranking.py" lines="24-66" />

**Observability.** `logger.debug('Reranking model unavailable, skipping: %s', e)` at construction time; per-query traces show the reranker phase elapsed in zero time when disabled.

## LLM circuit breaker

**Failure trigger.** `failure_threshold` consecutive failures recorded via `record_failure` (default tracked in `CircuitBreakerConfig`).

**Behaviour.** State machine: `CLOSED → OPEN → HALF_OPEN → CLOSED|OPEN`. Once OPEN, every call rejects with `CircuitBreakerOpen` until `reset_timeout_seconds` elapses; the next call transitions to HALF_OPEN and runs a single probe. Success resets to CLOSED; failure re-opens immediately. <code-ref path="packages/core/src/memex_core/circuit_breaker.py" lines="64-117" />

**Retry policy.** The breaker itself does not retry — it short-circuits to spare a failing provider. Callers decide whether to retry the underlying operation.

**Observability.** `Circuit breaker transitioning OPEN -> HALF_OPEN (probe allowed)` (INFO); `Circuit breaker opened after N consecutive failures` (WARNING); `time_until_reset` on the raised exception. <code-ref path="packages/core/src/memex_core/circuit_breaker.py" lines="33-39" />

## LLM extraction on a chunk

**Failure trigger.** Any chunk-level extraction error — DSPy parse failure, provider 5xx, transient network — except `OutputTooLongException`, which always re-raises.

**Behaviour.** `extract_facts_from_chunks` runs chunk extractors with `asyncio.gather(..., return_exceptions=True)` and partitions results into facts and errors. If at least one chunk succeeded, the function logs `%d/%d chunk(s) failed extraction, proceeding with %d facts from remaining chunks` and returns. Only when **every** chunk fails does it raise `ExtractionError`. <code-ref path="packages/core/src/memex_core/memory/extraction/core.py" lines="864-889" />

**Retry policy.** No in-process retry. The caller decides whether to re-ingest the document; content-defined chunking + hash-diffing means re-ingestion only resends the failed chunks.

**Observability.** WARNING log line with the failure ratio; chunk-level traces in OpenTelemetry; LLM call counter increments still apply (the call did happen).

## Embedding service

**Failure trigger.** ONNX session unavailable, LiteLLM backend rejects, configured backend type unknown.

**Behaviour.** The embedding loader raises directly (`ValueError(f'Unknown embedding backend: {type(config)}')` for unknown backends; backend-specific errors propagate). Ingestion blocks because every persisted fact carries a 384-dim vector — there is no degraded write path. Reads that do not need new embeddings (e.g., metadata-only lookups, KV reads) proceed unaffected. <code-ref path="packages/core/src/memex_core/memory/models/embedding.py" lines="60-68" />

**Retry policy.** Caller-driven. The ingestion service surfaces the exception so the agent or human operator can decide.

**Observability.** Backend-specific error propagates to the FastAPI route handler and is returned as HTTP 5xx; no metric gates this path because the operating assumption is that embedding is required.

## Reflection task LLM call

**Failure trigger.** Any exception raised inside the reflection coroutine after the queue row is claimed.

**Behaviour.** The queue-service `mark_failed` path locates the matching row with `SELECT ... FOR UPDATE SKIP LOCKED`, increments `retry_count`, and writes the truncated error to `last_error`. When `retry_count >= max_retries` (default `3`), the row moves to `DEAD_LETTER`; otherwise it returns to `FAILED` and the next scheduler tick reclaims it. <code-ref path="packages/core/src/memex_core/memory/reflect/queue_service.py" lines="544-564" />

**Retry policy.** `max_retries=3` per row, configured on `ReflectionQueue.max_retries` with `server_default='3'`. Dead-lettered rows are retained for inspection and can be replayed via `retry_dead_letter`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1320-1324" />

**Observability.** `Reflection task for entity %s failed (retry %d/%d)` (INFO) and `Reflection task for entity %s moved to dead letter after %d retries` (INFO); `last_error` column on the row; queue depth metric.

## Phase 5 CAS abandon

**Failure trigger.** A concurrent reflection worker advanced the `MentalModel` version column between this worker's read and its CAS UPDATE. This is benign — the other worker won the race.

**Behaviour.** `mark_abandoned` re-enqueues the queue item without incrementing `retry_count`. `last_error` is set to `CAS abandon (concurrent refresh won)` only when no prior real failure occupies the field, so consecutive abandons do not displace genuine error history. The next scheduler tick re-claims and the work proceeds against the now-current model. <code-ref path="packages/core/src/memex_core/memory/reflect/queue_service.py" lines="452-484" />

**Retry policy.** Unbounded by `max_retries` (because no count bumps). Repeated CAS abandons indicate sustained contention, not a fault — the system is doing what it should.

**Observability.** `memex_reflection_cas_abandons_total` counter (declared as `REFLECTION_CAS_ABANDONS_TOTAL`); `Reflection task for entity %s re-enqueued after CAS abandon (retry_count unchanged)` (INFO). <code-ref path="packages/core/src/memex_core/metrics.py" lines="63-67" />

## Per-entity Postgres advisory lock

**Failure trigger.** Another worker already holds the per-entity lock (`pg_try_advisory_lock` returns false) and the bounded wait elapsed without acquiring.

**Behaviour.** `acquire_entity_lock` spins on `pg_try_advisory_lock` every `_RETRY_INTERVAL_SECONDS` (0.1s) until `timeout_seconds` (default 30s) elapses, then raises `EntityLockTimeoutError(timeout_seconds=...)`. The error carries the configured timeout so HTTP handlers can derive a sensible `Retry-After`. Process crashes auto-release the lock when the Postgres backend terminates. <code-ref path="packages/core/src/memex_core/services/locks.py" lines="100-173" />

**Retry policy.** Caller retries with backoff. For `memex_memory_reconsolidate`, surface as HTTP 409 "another reconsolidation is in progress".

**Observability.** `memex_entity_lock_acquires_total{outcome='acquired'|'timeout'}`; `EntityLockTimeoutError` propagates to the route handler. The reconsolidate path also records `memex_reconsolidate_total{outcome='lock_timeout'}`. <code-ref path="packages/core/src/memex_core/services/locks.py" lines="129-160" />

## Per-entity asyncio lock (intra-worker)

**Failure trigger.** Two coroutines in the same worker process try to reflect on the same entity concurrently.

**Behaviour.** `get_entity_lock` returns the canonical `asyncio.Lock` for the entity from a `WeakValueDictionary`. The second coroutine waits until the first exits the lock context. Cross-worker dedup is handled by the queue's `SELECT ... FOR UPDATE SKIP LOCKED` claim — this lock is not visible across processes. <code-ref path="packages/core/src/memex_core/memory/reflect/entity_locks.py" lines="29-49" />

**Retry policy.** None needed — the lock is held briefly and released deterministically on context exit.

**Observability.** No dedicated metric; `_registry_size_for_tests` exposes registry size for assertions.

## Per-vault FSFM auto-band lock

**Failure trigger.** A brief leader flap caused two workers to enter `auto_deprioritize_after_lint` for the same vault simultaneously. `pg_try_advisory_xact_lock(int4, int4)` returns false for the contender.

**Behaviour.** The non-blocking try-form returns immediately with `lock_acquired = False`. The contending tick records `summary.skipped_lock_held = True` and increments `memex_fsfm_auto_band_skipped_total{reason='lock_held'}`. The lock is transaction-scoped — it releases on COMMIT or ROLLBACK. <code-ref path="packages/core/src/memex_core/api.py" lines="731-742" />

**Retry policy.** No retry on this tick — the contender waits for the next scheduled tick (`periodic_lint_task` cadence).

**Observability.** `memex_fsfm_auto_band_skipped_total{reason='lock_held'}` counter and `memex_fsfm_scorer_runs_total{outcome='skipped_locked'}` mark the skip. <code-ref path="packages/core/src/memex_core/metrics.py" lines="595-601" />

## FSFM auto-band cooldown

**Failure trigger.** A `memory_restore` audit row exists for the candidate unit within the last `cooldown_days` window.

**Behaviour.** Before applying the band, the auto-band loads `cooldown_unit_ids` via a `JOIN` on `audit_logs` and `memory_units` filtered to the active vault. Any candidate whose `target_id` is in that set is appended to `summary.skipped_cooldown` and skipped. <code-ref path="packages/core/src/memex_core/api.py" lines="785-838" />

**Retry policy.** The candidate becomes eligible again `cooldown_days` after its last restore. The pending `MaintenanceProposal` row stays pending until either consumed or evicted.

**Observability.** `memex_fsfm_auto_band_skipped_total{reason='cooldown_active'}` counter; `AutoDeprioritizeSummary.skipped_cooldown` lists the skipped unit ids.

## Surprise-gated lint cost cap

**Failure trigger.** The rolling-24h LLM cost counter for the vault would exceed `lint_llm.cost_cap_per_24h` if this unit were processed.

**Behaviour.** `check_and_increment_quota` runs a single atomic SQL statement (CTE-based rolling-window sum plus `ON CONFLICT` increment). When the predicate rejects, the unit is deferred via `defer(unit_id, vault_id, reason='cost_cap_exceeded', surprise_score=...)`, which writes a `MaintenanceProposal` row with `rule_name=_RULE_LLM_DEFERRED`. The deferred queue is capped at `deferred_queue_cap` — excess oldest rows are evicted (non-destructive, audit-preserving). <code-ref path="packages/core/src/memex_core/services/lint_llm.py" lines="425-519" />

**Retry policy.** `process_deferred` drains the queue FIFO on every subsequent tick that has headroom. Rule-only lint continues independently of this budget. <code-ref path="packages/core/src/memex_core/services/lint_llm.py" lines="710-720" />

**Observability.** `lint_llm_quota` table holds the per-hour bucket counters; deferred rows carry `evidence.reason='cost_cap_exceeded'` and the surprise score that triggered the deferral.

## Rule-only lint pass

**Failure trigger.** Independent of any LLM gate — runs even when `lint_llm.enabled=False` or the cost cap is exhausted.

**Behaviour.** `periodic_lint_task` runs `api.lint.run_rules(vault.id)` against every vault under `MEMEX_LEADER_LOCK_ID`. Per-vault failures are warning-logged and never raise — one bad vault does not stop the rest. The FSFM auto-band runs in the same task immediately after, gated by the per-vault advisory lock above. <code-ref path="packages/core/src/memex_core/scheduler.py" lines="280-327" />

**Retry policy.** Next scheduled tick. No per-row retry — the rule registry is idempotent on re-evaluation.

**Observability.** `Scheduler: Lint emitted %d findings in vault %s` (INFO); per-vault failure WARNING with the exception text.

## Consolidation orchestrator

**Failure trigger.** A per-entity step fails (contradiction detection, reflection, or stale-evidence prune), or the per-entity advisory lock times out.

**Behaviour.** `ConsolidationService.tick` iterates entities sequentially. On `EntityLockTimeoutError`, the entity is added to `entities_deferred` and the tick continues with the next entity. On any other per-entity exception, the first error is captured in `error` (recorded on the `ConsolidationTick` summary row) and processing continues. The tick-summary row is always written, so partial progress is durable across crashes. <code-ref path="packages/core/src/memex_core/services/consolidation.py" lines="160-238" />

**Retry policy.** The 500-unit-per-tick budget (`DEFAULT_TICK_BUDGET`) bounds work per pass; the next tick picks up where this one left off via the `last_tick` timestamp on the prior `ConsolidationTick` row. Deferred entities re-enter via the same diff query on the next tick. <code-ref path="packages/core/src/memex_core/services/consolidation.py" lines="56-62" />

**Observability.** `consolidation.tick.entity_deferred` (INFO) with `reason='entity_lock_timeout'`; `consolidation.tick.entity_error` (WARNING) with `exc_info=True`; `ConsolidationTick.error` column for post-hoc inspection.

## Scheduler leader lock (DB connection lost)

**Failure trigger.** The dedicated asyncpg connection used to hold `MEMEX_LEADER_LOCK_ID` reports `conn.is_closed()`.

**Behaviour.** The leader loop polls `conn.is_closed()` every 5 seconds while AioClock runs. On a closed connection the loop logs `Scheduler: Lost Postgres connection! stepping down...` (ERROR), cancels the AioClock task, releases the lock if still possible, sleeps 10 seconds, and re-enters the outer poll. Postgres auto-releases the advisory lock when the backend terminates, so other workers can acquire it immediately. <code-ref path="packages/core/src/memex_core/scheduler.py" lines="604-656" />

**Retry policy.** Same loop re-opens a fresh asyncpg connection and re-attempts `pg_try_advisory_lock`. Followers poll on a 60-second cadence; the new leader is chosen by whichever worker wins the next lock attempt.

**Observability.** `Scheduler: Lock acquired. I am LEADER. Starting AioClock...` and the step-down ERROR line bracket every leadership change.

## FSFM SQL↔Python parity (test gate)

**Failure trigger.** The composite score computed by the SQL CTE in `services/lint.py` diverges from the Python composite in `services/deprioritize_score.py` by more than 1e-4 on a synthetic unit.

**Behaviour.** This is a build-time correctness gate, not a runtime fail-open. The integration test seeds a varied fleet of synthetic units, runs both paths, and asserts the two `composite_score` values agree across every unit. A failing test blocks merge, surfacing drift before it lands. <code-ref path="packages/core/tests/integration/services/test_int_fsfm_sql_python_parity.py" lines="1-44" />

**Retry policy.** Not applicable — reconcile the two implementations.

**Observability.** Pytest output names the diverging unit and the absolute delta; CI annotates the failing assertion line.

## See also

- [Tutorial: Memory worth and deprioritization](../tutorial/memory-worth-and-deprioritization.md)
- [How-to: Configure reranker and embedding models](../how-to/configuring-server/reranker-and-embedding-models.md)
- [Reference: Observability](observability.md)
- [Explanation: Design principles](../explanation/design-principles.md)
