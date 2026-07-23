# retrieval-provenance-trace: persist a per-query retrieval trace so outcomes can target the units that actually surfaced

## 1. Title

Persist, per `memex_memory_search` query, a retrieval-provenance trace
(query text, returned memory-unit ids, per-unit rank/score, timestamp,
vault + session context), mint a `trace_id`, and surface it back on the
search response — so a later `memex_record_outcome` can reference the
units that were actually retrieved instead of firing a fresh
`limit=30` `memex_memory_search` and guessing. This is the "retrieval
provenance tracking" prerequisite RFC #254 calls out as "must come
first." Persistence + read-back + minimal surfacing ONLY — no
credit-assignment math.

## 2. Size / Effort

**S–M.** One new append-only table + alembic migration, one new
service that writes the trace at a single retrieval chokepoint, a
`trace_id` field threaded through two existing serialization boundaries
(core DTO and MCP model), one read-back path, and one simple prune job.
Effort is dominated by (a) getting the write at the one chokepoint
where the final ranked units exist, (b) the migration + its
Postgres-real test, and (c) surfacing `trace_id` back across the
streaming HTTP boundary without a wider envelope change. No arithmetic,
no scoring changes, no new agent-behavior surface beyond one schema
field.

## 3. Triggered by

RFC #254 (`/home/vscode/workspace/.temp/issues/rfc-254.md`),
"Temporal Credit Assignment." Line 13 of the RFC: *"Retrieval
provenance tracking: record (action, retrieved_units) pairs across the
task, forming a retrieval trace. This is the prerequisite and must come
first."* Triage scoped this ticket to ONLY that prerequisite slice.

Concretely, the gap this closes lives at
`packages/core/src/memex_core/services/outcomes.py:137`:
`record_outcome` receives `retrieved_set_size` as a bare `int` with NO
record of *which* units were retrieved. The `OutcomeAuditLog` row
written at `outcomes.py:322-335` stores `retrieved_set_size`,
`coverage_ratio`, and the per-unit verbs the caller *chose to classify*
— but nothing ties an outcome back to the ranked set a query actually
returned. An agent that wants to stamp "that worked" today must
re-run a wide `memex_memory_search` and guess which units it saw.

## 4. Context (today's state, cited)

### The single retrieval chokepoint

`memex_memory_search` (MCP) → `RemoteMemexAPI.search`
(`packages/common/src/memex_common/client.py:474-527`, which
`_post('memories/search', ...)` at `:526` and parses each ndjson row
into a `MemoryUnitDTO` at `:527`) → HTTP route `search_memories`
(`packages/core/src/memex_core/server/retrieval.py:34-109`) →
`api.search` → `SearchService.search`
(`packages/core/src/memex_core/services/search.py:58-121`) →
`self.memory.recall(session, request)`
(`packages/core/src/memex_core/memory/engine.py:291-330`).

`SearchService.search` is the tightest chokepoint that (a) has the
*resolved* vault scope (`vaults`, `search.py:86-98`), and (b) holds the
final ranked `list[MemoryUnit]` returned from `recall`
(`search.py:120-121`). It is called only by the memory-search path;
`survey` uses `recall` directly (`search.py:285-296`) and `note_search`
is a separate engine (`search.py:206`), so hooking here scopes the
trace to `memory_search` WITHOUT catching survey or note-search.

### Rank is reliable; score is often NULL on this path

On the memory-search path the rerank composes a `boosted` score but
**discards it** — `_rerank_results` returns
`[item[0] for item in scored_results]` at
`packages/core/src/memex_core/memory/retrieval/engine.py:2004`, keeping
only ordering. `build_memory_unit_dto` therefore reads
`score=getattr(unit, 'score', None)` →
`None`(`packages/core/src/memex_core/server/common.py:363`). (This
drop is the subject of a separate spike,
`.loop/plans/retrieval-staleness-score-spike.md`; DO NOT fix it here.)
Consequence for THIS ticket: **rank (list position) is the load-bearing
provenance signal; `score` must be captured when present but recorded
as nullable.**

### Session / caller identifier is already threaded

`packages/core/src/memex_core/context.py:21-30` — `get_session_id()`
returns the request-scoped session id (set from the `X-Session-ID`
header at `server/__init__.py:354-357`, default `'global'`). This is
the same identifier already accepted as `caller_id` on
`record_outcome` (`server/outcomes.py:91`, forwarded to
`OutcomeAuditLog.caller_id`). Reuse it — do not invent a new one.

### The closest existing audit mechanism (and why it is not enough)

`OutcomeAuditLog`
(`packages/core/src/memex_core/memory/sql_models.py:1648-1721`) is
written once per `record_outcome` at
`services/outcomes.py:322-335`. It captures `vault_id`, `caller_id`,
`units` (JSONB list of `{unit_id, verb, reason}`), `turn_outcome`,
`retrieved_set_size`, `coverage_ratio`, `exploration_tagged`,
`created_at`. It is an *outcome-time* record of the units the caller
*classified* — it has no query text, no ranked list, and is written on
a different event (outcome, not retrieval). The generic `AuditLog`
(`sql_models.py:1595-1645`) is a security-event trail
(`action`/`resource_type`/`resource_id`/`details` JSONB). Neither
models "here is the ranked set query Q returned at time T." → **a new
table is the right call** (see §11 Q1).

### Surfacing boundary

The HTTP response is an ndjson stream of `MemoryUnitDTO`
(`server/retrieval.py:107` via `ndjson_response`,
`server/common.py:378-409`), and `RemoteMemexAPI.search` reconstructs
DTOs and **discards response headers** (`client.py:527`). So a
header-based `trace_id` would require a client change too. The MCP tool
returns `list[McpFact | McpEvent | McpObservation]`
(`packages/mcp/src/memex_mcp/server.py:1710`) built by
`_build_memory_unit_model` (`server.py:1436-1495`, base kwargs at
`:1479-1495`), whose base carries `score`/`staleness` fields already
(`packages/mcp/src/memex_mcp/models.py:117,126`). The
convention-matching, minimal surfacing path is a nullable `trace_id`
field carried identically on every unit of a response (mirrors how
`score` rides the DTO), threaded DTO → MCP model (see §7, §11 Q2).

### Retention

The only existing periodic prune is KV TTL cleanup —
`periodic_kv_ttl_cleanup_task`
(`packages/core/src/memex_core/scheduler.py:250-258`) registered at
`scheduler.py:587-590` via `@clock.task(trigger=Every(seconds=300))`.
There is no generic audit-log retention job today; `OutcomeAuditLog`
and `AuditLog` are unbounded. A new provenance table needs a simple
prune modeled on the KV pattern (see §11 Q4).

## 5. Non-goals / out of scope

A diff touching any of these is a scope violation:

- **No credit-assignment math, temporal discounting, or backward
  propagation** of an outcome signal through the trace.
- **No change to Memory Worth counters, confidence scoring,
  deprioritization scoring, or consolidation weighting.** Do not touch
  the counter arithmetic in `services/outcomes.py:246-302`,
  `compute_mw_*` (`outcomes.py:76-114`), `services/deprioritize_score.py`,
  or `services/consolidation.py`.
- **No gap detection.**
- **Do NOT create** the RFC's later-slice modules
  `credit_assignment.py` or `confidence_update.py`.
- **Do NOT rewrite `record_outcome`'s counter arithmetic.** At most,
  `record_outcome` MAY accept and resolve an optional `trace_id` — but
  only if it lands cleanly (see §11 Q3); default is to DEFER trace
  *consumption* to a follow-up and keep this ticket to persistence +
  read-back + surfacing.
- **No fix to the dropped memory-path `score`**
  (`engine.py:2004`) — that is a separate spike.
- **No new MCP read tool** for traces. Read-back is a core api method +
  a thin HTTP GET route only (for tests + future consumption).
- **No new config knobs** beyond a single retention setting if one is
  needed (see §11 Q4) — reuse existing config patterns.

## 6. Requirements & restrictions

**Must achieve:**

- **R1 — Persist a trace per memory-search query.** At the
  `SearchService.search` chokepoint (`services/search.py:120-121`),
  after `recall` returns and BEFORE returning to the route, mint a
  `trace_id` (uuid4) and write ONE append-only row capturing: `query`
  text, ordered `units` payload of `{unit_id, rank, score}` (rank =
  0-based list position; `score = getattr(unit, 'score', None)`, may be
  NULL per §4), `created_at` timestamp, resolved vault scope, and
  `session_id` (from `get_session_id()`, `context.py:21`). Write once
  per query; never per unit.
- **R2 — Mint and surface `trace_id` back.** The `trace_id` must reach
  the agent on the search response. Attach it to each returned unit
  (dynamic attribute on the `MemoryUnit`, mirroring how
  `document_search` attaches `score`), thread it through
  `build_memory_unit_dto` (`server/common.py:363` region) onto a new
  nullable `MemoryUnitDTO.trace_id`
  (`packages/common/src/memex_common/schemas.py:531+`), and through
  `_build_memory_unit_model` (`server.py:1479-1495`) onto a new
  nullable `McpMemoryUnitBase.trace_id` (`mcp/models.py:111-126`). The
  value is identical across all units of one response.
- **R3 — Read-back path.** Provide `MemexAPI.get_retrieval_trace(
  trace_id)` returning the persisted row (or `None`), plus a thin HTTP
  GET route so a remote caller can fetch it. This is what a future
  `record_outcome` consumption slice and the integration test read.
- **R4 — Retention.** Add a simple prune for the new table modeled on
  `periodic_kv_ttl_cleanup_task` (`scheduler.py:250-258`, registered at
  `:587-590`): age-based delete OR cap-rows-per-vault (see §11 Q4).
  Traces must not grow unbounded.
- **R5 — Vault-scoped isolation.** The trace row is vault-scoped;
  provenance must never leak across tenants (same invariant as
  `OutcomeAuditLog`, `sql_models.py:1653`). See §11 Q5 for the
  multi-vault-search case.

**Restrictions (repo principles, cited):**

- **`.claude/rules/python-testing.md`** (`all-code-needs-tests`,
  `tests-are-real-code`, `dont-mock-what-you-can-run`): every code
  change ships a test; tests are typed + linted; no `skip`/`xfail`/
  `# type: ignore` to green a gate. **New table → integration test
  against real (testcontainer) Postgres**, per CLAUDE.md testing tiers
  ("Root tests — E2E against real Postgres via testcontainers"). See §8
  for the gate-visibility caveat.
- **CLAUDE.md migration convention**: schema change → alembic
  migration via `just db-revision "<message>"` (`justfile:189`),
  applied with `just db-upgrade` (`justfile:177`). Follow the numbered
  `NNN_slug.py` head-chaining convention (latest head is
  `069_nodes_chunks_search_tsvector`; new revision must set
  `down_revision = '069_...'` — verify the head at implementation time
  with `uv run alembic heads`).
- **CLAUDE.md agent-surface-tiers constraint** (`packages/mcp` per-tool
  ≤1,200 char cap): adding a `trace_id` *model field* is fine and does
  NOT touch a tool description. Do NOT expand any tool `description=`
  string. If any description text is unavoidable, keep within budget;
  the enforcing tests are `packages/mcp/tests/test_description_budgets.py`
  and `test_no_universal_content_in_descriptions.py` (NOT run by the
  loop gate — see §8; run manually).
- **`.claude/rules/pre-existing-issues.md`**: if the work surfaces an
  adjacent defect (e.g. the dropped `score` at `engine.py:2004`), record
  it — do not silently work around it and do not scope-creep a fix into
  this ticket.
- **`.claude/rules/adversarial-reviews.md`**: run an adversarial
  sub-agent review before declaring done — verify the trace is written
  exactly once per query, the `trace_id` round-trips to the MCP
  response, vault isolation holds, and no counter/scoring code moved.
- **Surgical-changes / simplicity (CLAUDE.md §2–3)**: minimum code;
  no speculative credit-assignment scaffolding; match existing style
  (single quotes, line length 100, async I/O, strict mypy).

## 7. Code surface (files + anchors + change per file)

New code:

- `packages/core/src/memex_core/memory/sql_models.py` — **add**
  `RetrievalTrace(SQLModel, table=True)` after `OutcomeAuditLog`
  (`:1721`). Columns: `id` (uuid pk, `gen_random_uuid()` server
  default, mirror `OutcomeAuditLog.id` at `:1658-1662`), `vault_id`
  (`vault_id_field()`, `:1663`), `session_id` (nullable str ≤128,
  mirror `caller_id` at `:1664-1669`), `query` (Text), `units` (JSONB
  `list[{unit_id, rank, score}]`, mirror `OutcomeAuditLog.units` +
  its array validator/CheckConstraint at `:1670-1686,1717-1720`),
  `created_at` (`TIMESTAMP(tz)` server default `now()`, mirror
  `:1708-1712`). Index on `(vault_id, created_at DESC)` (mirror
  `:1714-1716`).
- `packages/core/src/memex_core/alembic/versions/070_retrieval_trace.py`
  — **new migration** creating `retrieval_trace`; `down_revision`
  chains from the current head (`069_...`; verify with
  `uv run alembic heads`). Reversible `downgrade` drops the table.
- **New service** `RetrievalTraceService` (new file under
  `packages/core/src/memex_core/services/`, e.g.
  `provenance.py`) — `record_trace(session, *, vault scope, query,
  units, session_id) -> UUID` (mints `trace_id`, builds the
  `{unit_id, rank, score}` payload from list order + `getattr(u,
  'score', None)`, inserts the row) and `get_trace(session, trace_id)
  -> RetrievalTrace | None`. Wire into `MemexAPI` construction
  alongside the other services.

Modified:

- `packages/core/src/memex_core/services/search.py:120-121` — in
  `SearchService.search`, after `recall` returns, mint `trace_id`, call
  the trace write (using resolved `vaults` from `:86-98` and
  `get_session_id()`), and attach `trace_id` as a dynamic attribute on
  each returned `MemoryUnit` before returning. **Guard so a trace-write
  failure never breaks search** (log + continue; provenance is
  best-effort, retrieval is not).
- `packages/core/src/memex_core/api.py` — **add** thin facade methods
  `get_retrieval_trace(trace_id)` (delegates to the service, opens its
  own session like `record_outcome` at `:1857-1858`); wire the new
  service into `MemexAPI.__init__` next to `self._outcomes`/`self._search`.
- `packages/common/src/memex_common/schemas.py:531+` — **add**
  `trace_id: UUID | None = None` to `MemoryUnitDTO` (near `score` at
  `:583`).
- `packages/core/src/memex_core/server/common.py:348-369` — in
  `build_memory_unit_dto`, **add** `trace_id=getattr(unit, 'trace_id',
  None)` (next to `score` at `:363`).
- `packages/mcp/src/memex_mcp/models.py:111-126` — **add**
  `trace_id: UUID | None = None` to `McpMemoryUnitBase` (near `score`
  at `:117`).
- `packages/mcp/src/memex_mcp/server.py:1479-1495` — in
  `_build_memory_unit_model`, **add** `'trace_id': getattr(res,
  'trace_id', None)` to `base_kwargs`. (No tool-description edits.)
- **New HTTP GET route** for read-back, e.g. in a retrieval/provenance
  router mirroring `server/outcomes.py:111` (`post_record_outcome`)
  auth+vault-access pattern; returns the trace or 404.
- `packages/common/src/memex_common/client.py` — **add**
  `RemoteMemexAPI.get_retrieval_trace(trace_id)` calling the new GET
  route (needed by the integration test that drives the remote client).
- `packages/core/src/memex_core/scheduler.py` — **add**
  `periodic_retrieval_trace_prune_task` (model on
  `periodic_kv_ttl_cleanup_task`, `:250-258`) and register it
  (`:587-590` pattern).

Test files (see §8 for which gate runs which):

- `tests/test_retrieval_provenance_trace.py` — **new**, OFFLINE, runs
  under the loop gate `just test`.
- `tests/test_e2e_retrieval_provenance_trace.py` — **new**,
  `@pytest.mark.integration`, real Postgres; NOT run by the loop gate.

## 8. Tests & validation gates

**Gates (from `.loop/config.json`):**

- `just test` → `uv run pytest tests` with `addopts = -m 'not
  integration'` (`pyproject.toml:78`). **Verified this session:** this
  collects **56 of 264** root tests — every `@pytest.mark.integration`
  e2e test (208) is DESELECTED. The loop gate does NOT touch a real
  Postgres, and it does NOT run any `packages/*/tests` (only the root
  `tests/` dir). Plan test placement around this.
- `just prek` → ruff + mypy (strict). Fully typed; no `# type: ignore`.

**`require_eval: true`** — the loop refuses pickup until this ticket's
eval marker exists. The marker is authored and validated at
`.loop/evals/retrieval-provenance-trace.md` (`loopctl eval
retrieval-provenance-trace` → `valid`); keep it in step with this
section by hand if the DoD changes.

**Two-tier test plan (both required; each named test's home is in §7):**

1. **Loop-gate-visible, OFFLINE — `tests/test_retrieval_provenance_trace.py`.**
   Pure logic, no DB, so `just test` actually runs it:
   - **Payload construction:** given a synthetic ordered list of
     unit-like objects (some with a `.score`, some without), the
     `RetrievalTraceService` payload builder produces
     `[{unit_id, rank, score}]` with 0-based ranks in list order and
     `score=None` where absent — pinning that rank is captured
     reliably and score is nullable (§4).
   - **`trace_id` plumbing (DTO):** `build_memory_unit_dto` on a
     unit-like object with a dynamic `trace_id` attribute surfaces it on
     `MemoryUnitDTO.trace_id`; absent attribute → `None`.
   - **`trace_id` plumbing (MCP):** `_build_memory_unit_model` on a
     DTO-like object carrying `trace_id` surfaces it on the produced
     `McpFact`/`McpEvent`/`McpObservation`. (Import from `memex_mcp.server`
     as the existing MCP unit tests do; this file is in root `tests/`
     so the gate collects it.)
   - Parametrize inputs; inject any `now`; no wall-clock, no network,
     no Postgres.

2. **Integration, real Postgres — `tests/test_e2e_retrieval_provenance_trace.py`,
   `@pytest.mark.integration`.** Required by CLAUDE.md (new table → real
   testcontainer Postgres) but **NOT run by the loop gate** (it is
   deselected by `-m 'not integration'`). The implementer MUST run it
   manually before declaring done —
   `uv run pytest tests/test_e2e_retrieval_provenance_trace.py -m
   integration` — and the adversarial reviewer should too. Cases:
   - Migration applies cleanly (fresh DB) and `retrieval_trace` exists.
   - A `memory_search` (via `RemoteMemexAPI` / the HTTP route, mirroring
     `tests/test_e2e_f29_outcomes_route.py`) writes exactly ONE trace
     row with the returned unit ids, ranks, timestamp, vault, and
     session id; the response carries a `trace_id`.
   - `get_retrieval_trace(trace_id)` round-trips the row.
   - Vault isolation: a trace in vault A is not readable/leaked under
     vault B.
   - Prune deletes rows past the retention policy and keeps recent ones.

**Bug-fix note:** this is a feature slice, not a bug fix, so no
"reproducing test first" applies. The gap it closes (§3) is a missing
capability, and the offline payload test is the closest characterization
of the behavior added.

**Manual gates the loop does NOT run (call out to reviewer):** the
integration test above, and — if any MCP surface text changed (it
should not) — `packages/mcp/tests/test_description_budgets.py`. Run both
by hand.

## 9. Risk assessment

- **Blast radius: moderate but contained.** A new additive table + one
  additive field on two serialization models + one write at a single
  chokepoint. The write is guarded (best-effort) so it cannot break
  retrieval. No existing counter/scoring/ranking code moves.
- **Reversibility: high.** Migration has a `downgrade` that drops the
  table; the `trace_id` fields default `None` (backward-compatible on
  the wire); the prune job and read-back route are additive. Revert =
  drop table + remove additive fields.
- **Likeliest failure modes:**
  1. **Wrong chokepoint / double-writes.** Hooking inside
     `memory.recall` (`engine.py:291`) would also fire for `survey`
     (`search.py:285-296`) and any recall caller, writing spurious
     traces. Hook in `SearchService.search` only (§4).
  2. **Latency / transaction coupling.** A synchronous trace insert in
     the request path adds a write. Keep it a single small insert; if it
     shares the search session, ensure it cannot roll back the read.
     Prefer best-effort isolation (its own commit or a guarded
     background write) — but a background write races a *very* fast
     follow-up `record_outcome` referencing the `trace_id`; if
     consumption is deferred (§11 Q3) the race is harmless. Decide
     explicitly (§11 Q6).
  3. **`trace_id` never reaches the agent.** The streaming HTTP boundary
     drops headers (`client.py:527`), so a header-based approach
     silently fails. The DTO-field path (§7) is the one that
     round-trips — verify end-to-end in the integration test.
  4. **Score confusion.** Capturing `score` as if reliable — it is NULL
     on this path (`engine.py:2004`). Rank is the real signal; record
     score as nullable and do not "fix" the drop here.
  5. **Unbounded growth.** Forgetting the prune (R4) turns every search
     into permanent storage. The prune + its test are load-bearing.
  6. **Integration test invisibility.** Writing only an integration test
     means the loop gate (`-m 'not integration'`) proves nothing about
     this change. The offline tier-1 tests are what keep the loop
     honest; the integration tier must be run manually.

## 10. Subtickets (ordered, dependency-aware)

1. **Schema + migration.** Add `RetrievalTrace` to `sql_models.py`
   (mirror `OutcomeAuditLog`); generate `070_retrieval_trace.py` via
   `just db-revision`, chaining from the verified current head; write
   `upgrade`/`downgrade`. → verify: `just db-upgrade` applies and
   reverses cleanly on a scratch DB.
2. **Trace service.** New `RetrievalTraceService` with `record_trace` +
   `get_trace`; unit-test the payload builder OFFLINE
   (`tests/test_retrieval_provenance_trace.py`). → verify: `just test`
   green; payload has 0-based ranks + nullable score.
3. **Write hook.** Call `record_trace` in `SearchService.search` after
   `recall` (guarded/best-effort); attach `trace_id` to each unit. →
   verify: no double-write for survey/note-search (code inspection +
   integration test).
4. **Surface `trace_id`.** Add the field to `MemoryUnitDTO`,
   `build_memory_unit_dto`, `McpMemoryUnitBase`, `_build_memory_unit_model`;
   extend the offline plumbing tests. → verify: `just test` green;
   MCP model carries `trace_id`.
5. **Read-back.** `MemexAPI.get_retrieval_trace` + HTTP GET route +
   `RemoteMemexAPI.get_retrieval_trace`. → verify: integration test
   round-trips + vault isolation.
6. **Retention.** Prune task modeled on
   `periodic_kv_ttl_cleanup_task`; register in scheduler. → verify:
   integration test asserts old rows deleted, recent kept.
7. **Integration test + adversarial review.** Author
   `tests/test_e2e_retrieval_provenance_trace.py`; run it manually
   (`-m integration`); run `just prek`; delegate an adversarial review.
   → verify: one-trace-per-query, `trace_id` round-trips to the MCP
   response, isolation holds, no scoring code moved.

## 11. Open questions (forks; each with a recommendation)

- **Q1 — New table vs. extend `OutcomeAuditLog`?**
  *Recommendation:* **new `retrieval_trace` table.** They model
  different events (retrieval vs. outcome), and `OutcomeAuditLog`
  (`sql_models.py:1648-1721`) has no query text and no ranked list; a
  ranked-set column bolted onto an outcome row would be NULL on every
  outcome that did not originate a search. Separate append-only table,
  structurally mirroring `OutcomeAuditLog` for consistency.

- **Q2 — How to surface `trace_id` across the streaming boundary?**
  Options: (a) nullable `trace_id` field on `MemoryUnitDTO` /
  `McpMemoryUnitBase`, identical across all units; (b) an HTTP response
  header; (c) a synthetic leading "system-hint" unit like the degraded
  banner (`server.py:1809-1824`). *Recommendation:* **(a).** The client
  already parses DTOs and drops headers (`client.py:527`), so (b) needs
  a client change; (c) is noisier and burns a result slot. (a) matches
  how `score`/`staleness` already ride the unit and is the smallest wire
  change. Operator may prefer (c) if per-unit field bloat is a concern.

- **Q3 — Should `record_outcome` consume `trace_id` in THIS slice?**
  The scope note permits it "only if it lands cleanly and minimally."
  `record_outcome` needs per-unit *verbs* (`units=[{unit_id, verb,
  reason}]`, `outcomes.py:42-68`), which a `trace_id` alone cannot
  supply — so it can at most validate membership / derive
  `retrieved_set_size`, not replace the `units` payload.
  *Recommendation:* **DEFER consumption to a follow-up slice.** Keep
  this ticket to persistence + read-back + surfacing. This also
  neutralizes the background-write race (Q6). If the operator wants a
  cheap forward-link now, the minimal safe step is a nullable
  `trace_id` column on `OutcomeAuditLog` that stores the referenced
  trace (correlation only, zero arithmetic) — but that is a second
  migration; default is defer.

- **Q4 — Retention policy: age-based vs. cap-per-vault?**
  *Recommendation:* **age-based TTL delete** (e.g. traces older than N
  days), modeled directly on `periodic_kv_ttl_cleanup_task`
  (`scheduler.py:250-258`) with the interval/threshold read from config
  like the other scheduler tasks. Simpler to reason about than a
  per-vault row cap and matches the existing prune shape. Operator sets
  N (suggest 30 days) and whether it is configurable.

- **Q5 — Multi-vault search: what vault does the trace record?**
  A `memory_search` can resolve to several vaults (`vault_ids` list /
  `'*'`, `search.py:86-98`), but `OutcomeAuditLog.vault_id` is single.
  *Recommendation:* record the **resolved search scope** — if it
  resolves to exactly one vault, store that `vault_id`; for a
  multi-vault search, store the scope as a JSONB list on the row (or a
  companion column) rather than silently picking the first, so
  isolation (R5) is honest. Operator confirms whether multi-vault
  provenance is in scope for the first slice or should be single-vault
  only (simplest) with multi-vault deferred.

- **Q6 — Synchronous vs. background trace write?**
  Synchronous adds a small write to the request path but guarantees the
  `trace_id` is persisted before the response returns; background
  removes latency but races a fast follow-up read. *Recommendation:*
  **synchronous, best-effort (guarded)** — the insert is tiny, and
  with consumption deferred (Q3) there is no urgency argument for
  background complexity. Revisit if profiling shows measurable latency.

---

**Eval marker:** `.loop/config.json` sets `require_eval: true` — the
loop refuses pickup until this ticket's eval marker exists. Co-author
the eval with the `create-eval` skill before implementation (the "eval
is the spec" step). Suggested eval assertions: (1) a `memory_search`
writes exactly one `retrieval_trace` row carrying query, ordered
`{unit_id, rank, score}`, timestamp, vault, and session id; (2) the
search response surfaces a `trace_id` that `get_retrieval_trace`
round-trips; (3) NO change to counter/scoring code
(`services/outcomes.py:246-302`, `compute_mw_*`, `deprioritize_score.py`,
`consolidation.py`) and NO `credit_assignment.py`/`confidence_update.py`
created; (4) the offline tests live in root `tests/` and pass under
`just test`, and the integration test exists and passes under
`-m integration`.
