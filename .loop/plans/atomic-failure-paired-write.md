# Atomic failure-path paired write (record_outcome + deprioritize)

## 1. Title

Make the FAILURE-path paired write structural: one operation that records a
`not_helpful` outcome AND deprioritizes the same unit subset in a single DB
transaction, so the pairing the host doctrine mandates
(`.claude/rules/memex-agent-surface.md:64`) no longer depends on the agent
remembering to issue two independent calls.

## 2. Size / Effort

**M.** The two service primitives already exist and are individually
correct; the work is (a) a session-sharing refactor so both writes commit in
one transaction, (b) an orchestration entry point threaded through the API,
HTTP route, MCP tool, and HTTP client, and (c) the observation-guard and
mixed-verb edge cases. No new table, no migration, no math change. Size is
driven by the four-layer surface (service → api → HTTP route + MCP tool →
client) and by the offline-vs-integration test split forced by the loop
gate excluding Postgres tests (§8).

## 3. Triggered by

Host resolution doctrine, step 4+5 "Paired writes"
(`.claude/rules/memex-agent-surface.md:62-64`): a user-confirmed FAILURE
must stamp BOTH `record_outcome(verb='not_helpful')` AND
`memory_deprioritize` on the SAME judged subset ("User-confirmed-fix stamps
BOTH", `memex-agent-surface.md:71`). Today these are two independent tool
calls held together only by agent discipline. Under load an agent records
the outcome and forgets the deprioritize (or vice-versa), leaving the unit
in an inconsistent state (failure-credited but still surfaced, or hidden but
uncredited). The request is to provide a single operation that performs both
together on the failure verb, so the pairing is structural.

## 4. Context

**The two primitives are independent today and cannot share a transaction.**

- `OutcomeService.record_outcome`
  (`packages/core/src/memex_core/services/outcomes.py:124`) already takes a
  caller-supplied `session` plus a `commit: bool` flag
  (`outcomes.py:141`), and folds all counter updates + audit rows into one
  flush/commit (`outcomes.py:346-349`). It classifies the batch into
  `helpful_ids` / `not_helpful_ids` / `not_used_ids` internally
  (`outcomes.py:202-211`) but does NOT return those id lists — the caller
  cannot learn which units were `not_helpful` from the return value
  (`outcomes.py:403-414`). The `commit=False` path is the existing
  precedent: lint-resolve folds an outcome into its own FOR UPDATE
  transaction (`outcomes.py:339-341`, api wiring at `api.py:1852-1858`).
- `UnitsService.set_unit_deprioritized`
  (`packages/core/src/memex_core/services/units.py:47`) delegates to
  `_flip_deprioritized` (`units.py:205`), which opens its OWN session
  (`async with self.metastore.session()`, `units.py:219`) and commits
  inside (`units.py:276`). It does NOT accept an external session, so it
  cannot currently be folded into the outcome's transaction. Its deprio path
  also enqueues one `refresh_observation` task per citing observation in the
  same session as the flag flip (`units.py:260-267`).
- `batch_set_unit_deprioritized` (`units.py:91`) flips many units in one
  UPDATE but likewise opens its own session (`units.py:125`) and — unlike
  the per-unit path — has NO observation-id guard.
- `restore_unit` (`units.py:181`) is the reversal of deprioritize; outcomes
  have no reversal (append-only ledger).

**The surface exposes the two operations as separate calls at every layer.**

- API facade: `record_outcome` (`api.py:1804`) and
  `deprioritize_memory_unit` (`api.py:1565`) are distinct methods.
- HTTP: `POST /api/v1/outcomes/record`
  (`packages/core/src/memex_core/server/outcomes.py:110`, request model
  `RecordOutcomeRequest` at `outcomes.py:50`) and `POST
  /api/v1/memories/{id}/deprioritize`
  (`packages/core/src/memex_core/server/memories.py:236-241`) are distinct
  routes.
- MCP: `memex_record_outcome`
  (`packages/mcp/src/memex_mcp/server.py:3881`, dispatch at `server.py:3961`)
  and `memex_memory_deprioritize` (`server.py:4017`, dispatch at
  `server.py:4046`) are distinct tools. The MCP layer, not the API, uses
  `RemoteMemexAPI` (`packages/mcp/src/memex_mcp/lifespan.py:54`), so any
  atomic behavior must live server-side behind ONE HTTP call — two HTTP
  calls can never share a DB transaction.
- HTTP client: `RemoteMemexAPI.record_outcome`
  (`packages/common/src/memex_common/client.py:852`) and
  `deprioritize_memory_unit` (`client.py:1190`) are distinct client methods.
  An `AsyncSession` cannot cross the HTTP boundary (`client.py:878-882`).

**What is wrong / missing:** there is no single operation that records
`not_helpful` and deprioritizes the identical subset atomically. The
doctrine's structural guarantee is enforced only by prose.

## 5. Non-goals / out of scope

- Do NOT change SUCCESS-path semantics. `verb='helpful'` records the outcome
  and does NOT deprioritize (`memex-agent-surface.md:63`, "No deprio"). The
  asymmetry is intentional; the new operation must deprioritize ONLY the
  `not_helpful` subset of a batch, never `helpful` or `not_used` units.
- Do NOT make outcomes reversible. The MW ledger stays append-only
  (`outcomes.py:8-13`, proposal action `reversible = False` at
  `packages/core/src/memex_core/services/proposal_actions/record_outcome.py`).
- Do NOT alter Memory Worth math (`compute_mw_score` / `compute_mw_boost`,
  `outcomes.py:76-114`) or the deprioritize/restore surface-state model.
- Do NOT remove or change the standalone `memex_record_outcome` /
  `memex_memory_deprioritize` / `memex_memory_restore` tools or their
  routes. Both primitives stay independently callable and independently
  reversible (deprioritize via `restore_unit`; outcomes not reversible). The
  new operation is a convenience/atomicity WRAPPER over both, NOT a merge of
  the two concepts — the orthogonality of the axes
  (`memex-agent-surface.md:66-69`) must survive.
- Do NOT introduce a new success/failure semantics for `not_used`. That verb
  is engagement-only and never deprioritizes.

## 6. Requirements & restrictions

Requirements:

- R1. Provide a single operation that, given a batch of per-unit verbs,
  records the outcome for the whole batch AND deprioritizes exactly the
  subset whose verb is `not_helpful`, with both effects in one DB
  transaction so a partial failure rolls back both.
- R2. Mixed-verb batches must be handled: units stamped `helpful` /
  `not_used` in the same call are recorded but NEVER deprioritized
  (`memex-agent-surface.md:63-64`).
- R3. Preserve axis orthogonality: reuse the existing `record_outcome` and
  deprioritize primitives; do not collapse them. Both remain callable
  standalone (§5).
- R4. Observation guard: a `not_helpful` target that is a virtual/observation
  UUID must be handled per the read-only-observation contract — the standalone
  deprioritize surfaces HTTP 400 with `source_memory_units`
  (`.claude/rules/memex-agent-surface.md:7` `observation_read_only`; service
  raise at `units.py:225-237`; MCP re-surfacing at `server.py:4054-4073`).
  The atomic op must not silently drop the guard. Exact behavior is a fork
  (§11 OQ4).
- R5. Vault-scoping invariant: the deprioritize half must be scoped to the
  same vault as the outcome half; cross-vault flips are rejected
  (`units.py:239-249`).

Restrictions (repo principles, each cited):

- Every code change ships a test; a bug/inconsistency fix writes a
  reproducing/behavior test first (`.claude/rules/python-testing.md`,
  `all-code-needs-tests`).
- Tests are real code: linted and type-checked; no `skip` / `xfail` /
  `# type: ignore` to green a gate (`python-testing.md`,
  `tests-are-real-code`).
- Surgical changes only; match existing style; remove only orphans your
  change creates (`CLAUDE.md` §3).
- Simplicity first: minimum code, no speculative flexibility (`CLAUDE.md`
  §2). Prefer reusing `record_outcome(commit=False)` and the existing
  deprioritize primitive over new machinery.
- Pre-existing issues encountered must be fixed, not skipped
  (`.claude/rules/pre-existing-issues.md`).
- Gates go through the task runner / `prek`, not bare tools
  (`.claude/rules/prek-code-quality.md`, `.claude/rules/python-testing.md`).
- Any tool-description / doc markdown edit runs the slop-scan layers
  (`.claude/rules/slop-scan-for-docs.md`).
- Finish with an adversarial review in a sub-agent before declaring done
  (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

The implementer resolves the exact shape from §11 OQ1 (flag vs. dedicated
method) and OQ2 (session-sharing) first; the anchors below are where each
layer's change lands under either shape.

- `packages/core/src/memex_core/services/units.py:47` /
  `units.py:205` (`set_unit_deprioritized` → `_flip_deprioritized`) —
  refactor to accept an OPTIONAL external `session` (mirroring
  `record_outcome`'s `session` + `commit` pattern) so the flip, the
  observation-refresh enqueue (`units.py:260-267`), and the audit write can
  join the outcome's transaction. When no session is passed, behavior is
  unchanged (owns its session + commit). Gated by OQ2.
- `packages/core/src/memex_core/api.py:1804` (`record_outcome`) — the
  orchestration site. Open one session, call `self._outcomes.record_outcome(
  session=..., commit=False, units=...)`, derive the `not_helpful` subset
  from `units` (the API layer holds the verbs), deprioritize exactly that
  subset on the SAME session, then commit once. Reuses the existing
  owned-vs-caller-session branch (`api.py:1852-1858`).
- `packages/core/src/memex_core/api.py:1565`
  (`deprioritize_memory_unit`) — if OQ2 takes the session-threading route,
  pass the shared session through here or call the service directly with it.
- `packages/core/src/memex_core/server/outcomes.py:50`
  (`RecordOutcomeRequest`) and `outcomes.py:110` (`post_record_outcome`
  dispatch at `outcomes.py:141`) — add the `deprioritize_failures` field
  (OQ1 flag shape) or a sibling route, and forward it. `extra='forbid'`
  (`outcomes.py:54`) means the new field MUST be declared or stale clients
  422.
- `packages/mcp/src/memex_mcp/server.py:3881` (`memex_record_outcome`,
  dispatch at `server.py:3961`) — expose the flag/parameter and, on the
  failure path, re-surface the observation-400 with `source_memory_units`
  the same way the standalone deprioritize tool does
  (`server.py:4054-4073`).
- `packages/common/src/memex_common/client.py:852`
  (`RemoteMemexAPI.record_outcome`) — add the new field to the request body.
- `packages/common/src/memex_common/tool_descriptions.py:31-73` — update the
  `record_outcome` / `deprioritize` tool descriptions so the failure-path
  pairing is documented as one operation (doc edit → slop-scan, §6).
- `tests/test_atomic_failure_paired_write.py` (NEW, root suite) — the
  loop-gating behavior test (§8). Offline / non-integration.
- `packages/core/tests/services/test_atomic_failure_paired_write_int.py`
  (NEW, mirror) — the real single-transaction / rollback test against
  testcontainer Postgres, `@pytest.mark.integration`, run manually (§8).

## 8. Tests & validation gates

**Eval marker (acceptance layer):** `.loop/evals/atomic-failure-paired-write.md`
— 6 deterministic scenarios at 100%. Guardrails: both-writes-together; partial-failure-persists-neither;
mixed-batch deprioritizes only not_helpful; success-path never deprioritizes.
Fork-dependent rows: atomicity semantics (Q2→true rollback), observation-UUID (Q4→surface-and-rollback).

Gates (verified this session):

- `just test` → `uv run pytest tests` (`justfile:65-66`). `addopts`
  carries `-m 'not integration'` (`pyproject.toml [tool.pytest.ini_options]`),
  so Postgres-backed e2e tests marked `@pytest.mark.integration` (e.g.
  `tests/test_e2e_f4_deprioritize.py`,
  `tests/test_e2e_f29_outcomes_route.py:85`) are EXCLUDED from the loop gate.
  The loop-gating test must therefore be OFFLINE.
- `just prek` (`.pre-commit-config.yaml` via `prek`) — lint + type-check.
- Integration tests run on purpose with `-m integration` + Docker; they
  are NOT part of the loop gate.

Tests to add:

- **Loop-gating (root `tests/test_atomic_failure_paired_write.py`, offline,
  non-integration).** This suite cannot reach Postgres, so it asserts the
  ORCHESTRATION contract via test doubles on the `MemexAPI` instance
  (monkeypatch/spy `OutcomeService.record_outcome` and
  `UnitsService.set_unit_deprioritized`):
  1. A mixed batch `[helpful, not_helpful, not_used]` records the outcome
     for all three AND calls deprioritize for EXACTLY the `not_helpful`
     unit_id — asserting `helpful` and `not_used` units are NOT
     deprioritized (R2). Use a precise negative assertion on the spy's
     recorded call args, not a bare `assert_not_called` (`python-testing.md`
     "Make negative assertions precise").
  2. An all-`helpful` batch never calls deprioritize (success asymmetry, R2).
  3. The standalone `record_outcome` (flag off / default) does NOT
     deprioritize — proves the default preserves existing behavior.
- **Atomicity mirror (`packages/core/tests/...`, `@pytest.mark.integration`,
  Postgres, run manually).** This is where TRUE atomicity is provable —
  the offline test above proves orchestration, not single-transaction
  rollback (this tension is real; see OQ5):
  1. Happy path: after the op, the `not_helpful` unit has `failure_co_count`
     incremented AND `is_deprioritized = true`, in one transaction.
  2. Rollback: inject a failure in the deprioritize half after the outcome
     write; assert NEITHER effect persists (no orphan `failure_co` bump, no
     flip). This is the core guarantee the ticket exists to deliver.
  3. Observation guard: a `not_helpful` target that is an observation UUID
     yields the OQ4-chosen behavior (surface 400 + `source_memory_units`,
     with the whole transaction rolled back), and the outcome bump does NOT
     persist.

Every test named here has its file declared in §7.

## 9. Risk assessment

- **Blast radius.** Touches the outcome-recording hot path
  (`record_outcome`) and the deprioritize primitive — both are load-bearing
  agent tools. The session-sharing refactor of `_flip_deprioritized`
  (`units.py:205`) is the riskiest change: it is also called by the
  standalone route, the proposal-action deprioritize, and FSFM paths. The
  optional-session refactor must be strictly additive (no behavior change
  when no session is passed).
- **Reversibility.** The deprioritize half is reversible (`restore_unit`,
  `units.py:181`); the outcome half is NOT. So a wrongly-fired atomic op
  leaves an un-undoable `failure_co` bump — which is exactly why the
  transaction must be atomic (rollback prevents a half-applied,
  partly-irreversible state). The code change itself is a normal revert.
- **Likeliest failure modes.** (a) The offline loop-gating test passes on
  orchestration while a real single-transaction bug ships, because Postgres
  is excluded from the gate — mitigated by the required integration mirror
  (§8, OQ5). (b) Deprioritizing `helpful`/`not_used` units by mis-deriving
  the subset — covered by test 1. (c) The observation-400 case bumping
  `failure_co` on a non-existent MU (record_outcome's UPDATE matches zero
  rows for an observation UUID) then failing at deprioritize — covered by
  the guard test; rollback keeps it clean. (d) A second commit inside
  `_flip_deprioritized` breaking the shared transaction if the refactor is
  incomplete.

## 10. Subtickets

1. **Session-threading refactor (OQ2).** Add an optional external `session`
   to `set_unit_deprioritized` / `_flip_deprioritized` (`units.py:47`,
   `units.py:205`), additive and behavior-preserving when absent. Ship its
   own unit + integration test. Depends on OQ2 being settled.
2. **API orchestration.** Implement the atomic path in
   `record_outcome` (`api.py:1804`): one session, outcome with
   `commit=False`, deprioritize the `not_helpful` subset on the same
   session, one commit. Includes the observation-guard behavior (OQ4).
   Depends on 1.
3. **Loop-gating offline test.** Add `tests/test_atomic_failure_paired_write.py`
   (orchestration via spies). Depends on 2.
4. **Surface exposure.** Thread `deprioritize_failures` (OQ1 shape) through
   the HTTP route (`server/outcomes.py:50`, `:141`), the MCP tool
   (`mcp/server.py:3881`) with observation-400 re-surfacing, and the client
   (`client.py:852`). Depends on 2.
5. **Docs.** Update `tool_descriptions.py` (`:31-73`) to describe the
   failure pairing as one operation; run slop-scan. Depends on 4.
6. **Integration mirror + adversarial review.** Add the Postgres atomicity /
   rollback / observation test (`packages/core/tests/...`), then run the
   sub-agent adversarial review. Depends on 2-5.

## 11. Open questions

- **OQ1 — Surface shape: flag vs. dedicated operation.** Two shapes:
  (a) a `deprioritize_failures: bool = False` flag on the existing
  `record_outcome` (API `api.py:1804`, route `outcomes.py:50`, MCP
  `server.py:3881`, client `client.py:852`); or (b) a dedicated API method +
  HTTP route + MCP tool (e.g. `memex_record_failure`) that always pairs.
  **Recommendation: (a) the flag.** It reuses the single batch entry point
  where verbs are already classified, so mixed-verb handling (R2) falls out
  for free; it keeps the standalone tools untouched (§5); and it is the
  smaller surface (`CLAUDE.md` §2). Tradeoff to note: a flag still relies on
  the agent setting it, so it hardens the pairing (one atomic call instead
  of two) without fully removing agent discretion — a dedicated
  always-pairs tool would remove it but fragments the surface and duplicates
  the batch plumbing. Default MUST be `False` to preserve every existing
  caller. Operator decides.
- **OQ2 — Single transaction vs. sequential-with-defined-failure.** Can both
  writes share one DB transaction? `record_outcome` already supports
  `commit=False` + external session (`outcomes.py:141`), but
  `_flip_deprioritized` opens its own session (`units.py:219`).
  **Recommendation: refactor `_flip_deprioritized` to accept an optional
  external session** (additive, behavior-preserving) and drive both writes
  on one session with a single commit — this is the only shape that
  delivers true rollback-on-partial-failure, which is the ticket's core
  guarantee (§9 reversibility). If the operator rejects the refactor, the
  ticket degrades to sequential calls and MUST specify the order and
  partial-failure semantics explicitly: record the outcome FIRST (append-only,
  the reversible half runs second so a deprioritize failure leaves an
  uncredited-but-reversible unflipped unit), and document that partial state
  is possible. Operator decides.
- **OQ3 — Deprioritize the subset per-unit or batched.** Use per-unit
  `set_unit_deprioritized` in a loop (`units.py:47`, has the observation
  guard `units.py:220-238` and inline observation-refresh enqueue
  `units.py:260-267`; N statements) or `batch_set_unit_deprioritized`
  (`units.py:91`, one UPDATE but NO observation guard).
  **Recommendation: per-unit loop within the shared session.** The
  observation-400 contract (R4) and the inline refresh enqueue matter more
  than the statement count; judged subsets are small (the doctrine's step-3
  "judged subset", `memex-agent-surface.md:61`), so N is tiny. Operator
  decides.
- **OQ4 — Observation-UUID target in the failure subset.** If a
  `not_helpful` unit is a read-only observation UUID, the standalone
  deprioritize raises 400 with `source_memory_units` (`units.py:225-237`,
  MCP re-surface `server.py:4054-4073`). Options: (i) surface the 400 and
  roll back the WHOLE transaction (including the outcome bump); (ii) auto-
  resolve to the source MUs and deprioritize those; (iii) skip that unit,
  keep the rest.
  **Recommendation: (i) surface-and-rollback.** It preserves atomicity and
  matches the standalone contract; (ii) would silently deprioritize a fact
  the caller never named (violates §5 surgical/no-surprise intent); (iii)
  leaves an inconsistent partial write, which is the exact failure this
  ticket removes. Operator decides.
- **OQ5 — Loop-gate cannot prove atomicity offline.** The loop gate excludes
  Postgres (`pyproject.toml` `-m 'not integration'`), so the root
  `tests/` test can only assert orchestration (which units get
  deprioritized), not single-transaction rollback. The true guarantee is
  provable only in the `@pytest.mark.integration` mirror under
  `packages/core/tests/`, which the loop does NOT run.
  **Recommendation: accept the split** — ship both, and treat the integration
  mirror as a required manual gate before merge (called out in §8 and
  subticket 6), since the offline test alone can green while a rollback bug
  ships (§9 failure mode (a)). Operator confirms this is acceptable, or
  promotes a lightweight in-process transaction test into the default gate.
