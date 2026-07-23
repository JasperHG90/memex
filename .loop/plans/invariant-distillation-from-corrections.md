# Invariant distillation from the correction stream

## 1. Title

Derive candidate behavioral invariants from Memex's accumulated
correction/outcome stream, propose them through the existing human-gated
proposal surface, and materialize approved ones with lineage back to the
correction events that produced them.

## 2. Size / Effort

**L.** Drivers: a new mineable-signal query layer, a new distillation pass
(LLM or heuristic) that must mirror the existing derivation architecture
rather than fork it, a new proposal-action + materialization target, a new
scheduled task, and a database migration for lineage. The size is dominated
by the go/no-go spike gating the rest and by the number of existing
subsystems the change must integrate with (outcome audit log, KV store,
maintenance-proposal ledger, scheduler) without touching the mental-model
derivation it deliberately parallels. Decomposed into 5 single-iteration
subtickets (§10).

## 3. Triggered by

The H-MEM "invariant primitive" and "human-gated proposal queue" ideas from
the Meterless comparison (memex vault notes `6fbf8fd8` and handoff
`8ed744aa`). Memex already STORES behavioral invariants (hand-written KV
entries such as `user:greeting`,
`global:procedure:assert:no-mock-method-call`) and already SYNTHESIZES
conceptual memory (mental models). The single missing layer is the
distillation-from-feedback loop: nothing today watches the accumulated
correction stream and proposes a recurring correction as a candidate rule.

## 4. Context

Today's state, by layer:

**The correction stream already persists and is queryable.**
- `record_outcome` writes one `OutcomeAuditLog` row per call with a
  per-unit `units` JSONB payload of `{unit_id, verb, reason}`
  (`packages/core/src/memex_core/services/outcomes.py:323`). The
  `not_helpful` verb is the primary failure signal
  (`outcomes.py:207`). The table is append-only, vault-scoped, and indexed
  `(vault_id, created_at DESC)`
  (`packages/core/src/memex_core/memory/sql_models.py:1648`,
  index at `sql_models.py:1715`).
- `record_outcome` also emits one `AuditLog` row per credit-bearing unit
  with `action='outcome.record'` and `details.outcome` in
  `{success, failure}` (`outcomes.py:307`).
- `memory_deprioritize` persists as an `AuditLog` row with
  `action='memory_deprioritize'`
  (`packages/core/src/memex_core/services/units.py:83`); restore writes
  `action='memory_restore'` (`units.py:198`). `AuditLog` is indexed by
  `action` and `timestamp` (`sql_models.py:1595`, indexes at
  `sql_models.py:1640`).
- Explicit stated preferences already land as KV entries via `kv_put`
  (`packages/core/src/memex_core/services/kv.py:83`).

So the three raw signals the ticket must mine (`not_helpful` outcomes,
`deprioritize` events, stated preferences) all have durable, indexed homes.
No new capture path is needed.

**The storage layer for invariants already exists.**
- `KVEntry` (`sql_models.py:1724`) with namespace prefixes
  `global:`/`user:`/`project:`/`app:` (validation at `kv.py:58`, namespaces
  sourced from `memex_common.kv_utils.VALID_NAMESPACES`, `kv.py:41`), unique
  on `key`, upserted via `INSERT ... ON CONFLICT DO UPDATE` (`kv.py:104`).
  Note: `KVEntry` has **no** provenance/lineage column today (fields at
  `sql_models.py:1741-1772` are `id, key, value, embedding, expires_at,
  created_at, updated_at`). Lineage must therefore be encoded in `value`
  or added as a new column (see §11 Q2).

**The human-gated proposal mechanism already exists and is the reuse
target.**
- `MaintenanceProposal` (`sql_models.py:2599`) is the finding ledger:
  `status` in `pending/resolved/dismissed`, `evidence` JSONB,
  `suggested_action`, `source` in `rule/llm/external`, read-only from the
  agent surface, with a unique partial index making reruns idempotent
  (`sql_models.py:2679`).
- `ProposalAction` is a Protocol + registry of canned, reversible mutations
  (`packages/core/src/memex_core/services/proposal_actions/base.py:61`,
  `register_action` at `base.py:155`, `get_action` at `base.py:172`). Each
  action carries `execute()`/`reverse()`/`preview()` and a `reversible`
  ClassVar.
- External emitters file a proposal with a pre-selected action via
  `insert_external_proposal(ExternalProposalRequest(...))`
  (`packages/core/src/memex_core/services/lint_external.py:84`,
  action validation at `lint_external.py:150`), passing
  `proposed_action={action_name, params}`.
- The human resolves through the lint surface: `lint_resolve`
  (`packages/core/src/memex_core/server/lint.py:822`), `lint_apply`
  (`server/lint.py:1327`), `lint_reverse` (`server/lint.py:1357`),
  catalogue at `server/lint.py:193`, submit at `server/lint.py:271`.

**The closest existing analogue to "derive a rule from accumulated
episodes" is the procedural strategy/procedure derivation.**
- `ProceduralDerivationService`
  (`packages/core/src/memex_core/services/procedural_derivation_service.py:64`)
  drains a dirty-cluster queue, distils cases into procedures and procedures
  into strategies, writes derived entries as `status='draft'`, and files a
  confirmation proposal on the lint surface rather than auto-activating
  (`_file_activation_proposal` at `procedural_derivation_service.py:238`,
  `_file_apply_derivation_proposal` at `:190`, thresholds
  `MIN_CASES_FOR_DISTILLATION` and `MIN_PROCEDURES_FOR_STRATEGY` at `:53`,
  rule name at `:57`). This draft-to-confirm-to-active shape is exactly the
  lifecycle this ticket needs, and its proposal-filing helpers are the
  template to copy.

**The scheduling harness already exists.**
- `packages/core/src/memex_core/scheduler.py` registers per-vault periodic
  tasks under a single leader lock via `@clock.task(trigger=Every(...))`:
  `periodic_consolidation_task` (`scheduler.py:373`, wired ~`:654`),
  `periodic_derivation_task` (`scheduler.py:395`, wired ~`:626`),
  `periodic_lint_llm_task` (`scheduler.py:413`, wired ~`:614`). A new
  distillation pass mirrors these.
- The API entry point pattern is `api.procedural.derive_pending(...)` →
  `ProceduralDerivationService(self._api).process_pending(limit=limit)`
  (`packages/core/src/memex_core/api.py:2623`).

**What is missing:** the distillation pass itself, its aggregation query
over the correction stream, the candidate-rule proposal, and the
materialization action with lineage. Nothing reads `OutcomeAuditLog` /
`AuditLog` to detect a recurring correction and propose a rule.

## 5. Non-goals / out of scope

- Do NOT modify the mental-model derivation. `reflect_batch`
  (`packages/core/src/memex_core/memory/reflect/reflection.py:275`) and the
  `MentalModel` table (`sql_models.py:138`) are parallel machinery to mirror,
  not to change.
- Do NOT auto-materialize any invariant. Materialization is only ever the
  result of an explicit human resolution on a proposal.
- Do NOT add a path that fabricates a `MemoryUnit`. Memory units are
  append-only facts extracted from notes (`sql_models.py:625`; storage-layer
  invariant in `.claude/rules/memex-agent-surface.md`, "Memory units —
  append-only facts extracted from notes. NEVER edit/replace/delete";
  DESIGN_DOCUMENT.md:46 P6). A derived invariant is not a memory unit.
- Do NOT re-implement the KV store, the outcome audit log, or the
  maintenance-proposal ledger. Reuse `KVService.put` (`kv.py:83`),
  `OutcomeAuditLog` (`sql_models.py:1648`), and `insert_external_proposal`
  (`lint_external.py`).
- Do NOT build distillation machinery before the signal-density spike
  (§10.1) returns a GO.

## 6. Requirements & restrictions

R1. **Human-approval gate is mandatory.** A distilled invariant governs
agent behavior and MUST NOT auto-materialize. The lifecycle is
proposed → (human approves/rejects) → materialized, filed on the existing
`MaintenanceProposal` ledger through `insert_external_proposal`
(`lint_external.py:84`) with a pre-selected `proposed_action`, exactly as
`ProceduralDerivationService._file_activation_proposal`
(`procedural_derivation_service.py:238`) does. This mirrors the repo's
established gate pattern; it also satisfies DESIGN_DOCUMENT.md:42 (P1 — the
user has the last say; agent-behavior changes are human-gated).

R2. **Rejections are logged; approvals carry lineage.** A rejection resolves
the proposal as `dismissed` (`LintStatus`, `sql_models.py:2587`). An
approval must record `derivedFrom` lineage back to the specific correction
events (the `OutcomeAuditLog.id` / `AuditLog.id` rows, or the `unit_id`s and
their reasons) that produced the candidate. Lineage MUST be persisted on the
materialized artifact, not only in the proposal's `evidence`, so it survives
after the proposal row is resolved. This satisfies DESIGN_DOCUMENT.md:46 (P6
— append-only with strict lineage and traceability).

R3. **Never fabricate a memory unit.** The materialized invariant MUST be
EITHER a KV entry (via `KVService.put`, `kv.py:83`) OR a new first-class
derived object carrying explicit synthesis provenance — NOT a synthesized
`MemoryUnit`. The choice between the two targets is a genuine fork (§11 Q2).

R4. **Signal-density is a go/no-go gate, not an afterthought.** The first
subticket (§10.1) MUST quantify the distillable signal in real vaults before
any distillation machinery is written. If the signal is too sparse, manual
`kv_put` remains simpler and the feature is not built. The proceed threshold
MUST be stated as an explicit criterion (recommendation in §11 Q1).

R5. **Mirror, do not fork, the derivation architecture.** The distillation
pass must follow the `ProceduralDerivationService` shape (queue/scan →
distil → draft → file confirmation proposal) and run as a leader-locked
periodic task in `scheduler.py` alongside the existing passes. It must not
introduce a parallel scheduler or a bespoke proposal ledger.

R6. **Reversible materialization.** The materialization `ProposalAction`
must implement `reverse()` (or declare `reversible = False` and short-circuit
per `base.py:117`). Prefer reversible: a KV materialization can capture the
prior value in `prior_state` and restore it, matching `ApplyDerivationAction`
(`apply_derivation.py:92`).

R7. **Vault scoping.** All mining and materialization is vault-scoped;
`OutcomeAuditLog` and the proposal ledger are already vault-partitioned
(`sql_models.py:1663`, `:2614`). No cross-tenant leakage.

R8. **Every code change ships with a test** that runs under the loop gate
(`.claude/rules/python-testing.md`; see §8 for the gate-collection caveat).

## 7. Code surface

New and touched files, each with the change in a clause. Line anchors point
at the insertion/reference site in existing files.

- `packages/core/src/memex_core/services/invariant_distillation.py` **(new)**
  — the distillation service: mine the correction stream, cluster recurring
  corrections, distil a candidate rule, file a confirmation proposal. Mirrors
  `procedural_derivation_service.py:64`.
- `packages/core/src/memex_core/services/correction_stream.py` **(new)** —
  the aggregation query layer over `OutcomeAuditLog` (`sql_models.py:1648`)
  and `AuditLog` `action IN ('memory_deprioritize','outcome.record')`
  (`sql_models.py:1595`), returning recurrence-counted correction clusters
  per vault. Used by both §10.1 (spike) and §10.3 (distillation).
- `packages/core/src/memex_core/services/proposal_actions/materialize_invariant.py`
  **(new)** — a registered `ProposalAction` (`base.py:61`, register via
  `base.py:155`) whose `execute()` writes the approved invariant to its
  materialization target with lineage and whose `reverse()` restores the
  prior state. Modeled on `apply_derivation.py:32` and
  `activate_procedural_entry.py`.
- `packages/core/src/memex_core/services/proposal_actions/__init__.py`
  (`proposal_actions/__init__.py:1`) — import the new action module for its
  registration side effect.
- `packages/core/src/memex_core/scheduler.py:395` — add
  `periodic_invariant_distillation_task` next to `periodic_derivation_task`,
  and wire it with `@clock.task(trigger=Every(...))` next to the derivation
  registration (`scheduler.py:626`).
- `packages/core/src/memex_core/api.py:2623` — add an `api.*.distil_pending`
  entry point mirroring `derive_pending`, so the scheduler and any CLI/HTTP
  trigger call the service through the API facade.
- `packages/core/src/memex_core/memory/sql_models.py:1724` — IF §11 Q2
  resolves to "new derived object", add the table here and a migration under
  `packages/core/src/memex_core/alembic/versions/`; IF it resolves to "KV
  entry with lineage column", add the lineage column to `KVEntry`
  (`sql_models.py:1741`) plus migration. Either way a migration is required.
- **Tests** (homes for every test named in §8):
  - `tests/test_e2e_invariant_distillation.py` **(new, root suite)** — the
    offline end-to-end behavior test that the loop gate `just test`
    collects (see §8 caveat). Sits alongside existing root e2e tests such
    as `tests/test_e2e_audit_logging.py`.
  - `packages/core/tests/unit/test_correction_stream.py` **(new)** — unit
    tests for the aggregation query (recurrence counting, vault scoping).
  - `packages/core/tests/unit/test_materialize_invariant_action.py`
    **(new)** — execute/reverse/validate/preview for the proposal action.
  - `packages/core/tests/integration/test_int_invariant_distillation.py`
    **(new)** — testcontainer-Postgres run of mine → distil → propose →
    resolve → materialize with lineage, marked `integration`
    (`pyproject.toml:80`).
  - `packages/core/tests/unit/test_signal_density_spike.py` **(new, §10.1)**
    — asserts the measurement script's counting logic against a fixture.

## 8. Tests & validation gates

**Eval marker (acceptance layer):** `.loop/evals/invariant-distillation-from-corrections.md`
— 7 deterministic scenarios at a 100% bar (3 guardrails: propose-not-materialize,
never-fabricate-a-unit, vault-scope; plus approve+lineage, reject=dismiss,
below-threshold anti-nag, spike GO/NO-GO). Row 2's lineage assertion is Q2-dependent.

**Repo gates (from `.loop/config.json` and the `justfile`):**
- `just test` → `uv run pytest tests` (`justfile` `test` recipe).
- `just prek` → `uv run prek run -a` (`justfile` `prek` recipe): ruff, mypy,
  formatting per `.pre-commit-config.yaml`.
- Default pytest deselects integration tests:
  `addopts = "... -m 'not integration'"` (`pyproject.toml:78`); markers at
  `pyproject.toml:79`. `asyncio_mode = "auto"` (`pyproject.toml:88`).

**Critical gate-collection caveat (verified, not assumed).** `just test`
runs `uv run pytest tests`, whose positional `tests` path collects ONLY the
root `./tests/` directory. A collect-only run yields 56 tests
(208 integration deselected) and ZERO from `packages/core/tests`. Therefore
unit/integration tests placed under `packages/core/tests/` are NOT exercised
by the loop's `just test` gate. Consequence for the implementer:
- The behavior test that must gate each loop iteration goes in root
  `./tests/` (hence `tests/test_e2e_invariant_distillation.py` in §7),
  offline and not `integration`-marked, so `just test` runs it.
- The mirrored package-level unit tests
  (`packages/core/tests/unit/...`) still belong there per
  `.claude/rules/python-testing.md`, but the implementer MUST run them
  explicitly (`uv run pytest packages/core/tests`) since the loop gate will
  not. This tension is surfaced as §11 Q5.

**Tests to add** (each has a declared home in §7):
- Go/no-go spike counting logic → `test_signal_density_spike.py` (§10.1).
- Correction-stream aggregation (recurrence, vault scoping, verb filter) →
  `test_correction_stream.py` (§10.2).
- Distillation clustering + candidate emission → covered in the integration
  test and the root e2e test (§10.3).
- Proposal filing + human resolve (approve → materialize, reject → dismiss)
  → `test_int_invariant_distillation.py` and
  `test_e2e_invariant_distillation.py` (§10.4).
- Materialization action execute/reverse + lineage persistence →
  `test_materialize_invariant_action.py` and the e2e test (§10.5).

**Eval-suites applicability.** `.claude/rules/eval-suites.md` requires
retrieval/extraction/agent-behavior regression checks to live as a suite
under `packages/eval/src/memex_eval/suites/<name>/`. This feature changes
what gets PROPOSED to a human, not retrieval ranking or extraction output,
so it is not obviously eval-suite-shaped. Assess during §10.4: if a
regression check on "which corrections produce which candidate invariants"
is wanted, it belongs in the eval framework, not in `tests/`. Surfaced as
§11 Q6.

## 9. Risk assessment

- **Blast radius.** Additive: a new service, a new proposal action, a new
  scheduled task, one migration. It reads the correction stream and the KV
  store and writes only proposals plus (on human approval) one KV entry or
  one derived row. It does not alter retrieval, extraction, outcome
  recording, or mental-model derivation. The scheduler change adds one
  leader-locked task that, like its siblings, warning-logs and never raises
  (`scheduler.py:387`).
- **Reversibility.** High if materialization is a reversible
  `ProposalAction` (R6). A bad candidate is dismissed before it ever
  materializes; a bad materialization is reversed via `lint_reverse`
  (`server/lint.py:1357`). The migration is the least reversible piece;
  keep it additive (new column/table, no drops).
- **Likeliest failure modes.** (1) Signal too sparse, making the whole
  feature noise — mitigated by the §10.1 go/no-go gate. (2) Over-proposing
  (nagging the human with weak candidates) — mitigated by a recurrence
  threshold (§11 Q1) and the ledger's pending-dedup unique index
  (`sql_models.py:2679`). (3) Lineage lost after proposal resolution if
  stored only in `evidence` — mitigated by R2 (persist on the artifact).
  (4) Tests placed only under `packages/core/tests` silently not gating the
  loop — mitigated by §8 caveat and the root e2e test.

## 10. Subtickets

Ordered, dependency-aware. Each is one loop iteration.

1. **Signal-density spike + go/no-go gate.** Write a measurement script (or
   a `memex-eval`/CLI one-shot) that, per real vault, counts: `not_helpful`
   outcomes in `OutcomeAuditLog` (`sql_models.py:1648`), `memory_deprioritize`
   events in `AuditLog` (`sql_models.py:1595`), and stated-preference KV
   writes, and reports the recurrence distribution (how many distinct
   corrections recur ≥N times). Assert the counting logic with
   `test_signal_density_spike.py`. Output a GO/NO-GO against the stated
   threshold (§11 Q1). **No distillation machinery is written until this
   returns GO.** Gate the remaining subtickets on it.
2. **Correction-stream query/aggregation layer.**
   `correction_stream.py` + `test_correction_stream.py`: a vault-scoped query
   returning recurrence-counted correction clusters (keyed by normalized
   reason/target), reused by the spike and the distillation pass. No behavior
   change yet.
3. **Candidate distillation.** `invariant_distillation.py`: consume clusters
   above the recurrence threshold, distil a candidate rule (heuristic first;
   LLM only if §11 Q3 says so), emit an in-memory candidate. Mirror
   `procedural_derivation_service.py:64`. Not yet wired to the scheduler.
4. **Proposal + human-gate lifecycle.** File the candidate as a
   `MaintenanceProposal` via `insert_external_proposal`
   (`lint_external.py:84`) with a pre-selected `materialize_invariant`
   action; wire the periodic task into `scheduler.py` (`:395`, `:626`).
   Approve → resolves and triggers materialization; reject → `dismissed`.
   Covered by `test_int_invariant_distillation.py` +
   `test_e2e_invariant_distillation.py`.
5. **Materialization with lineage.**
   `materialize_invariant.py` `ProposalAction`: `execute()` writes the
   approved invariant to its target (KV or derived object per §11 Q2) with
   `derivedFrom` lineage persisted on the artifact (R2); `reverse()` restores
   prior state (R6). Migration added. Covered by
   `test_materialize_invariant_action.py` + the e2e test.

## 11. Open questions

Q1. **Recurrence threshold and go/no-go criterion (§10.1).** What density
justifies building this? Recommendation: proceed only if at least one real
vault shows ≥5 distinct corrections that each recur ≥3 times over the
available history; set the distillation recurrence threshold to "same
correction cluster seen ≥3 times". If no vault clears this, NO-GO and close
the feature in favor of manual `kv_put`. Operator to confirm the numbers
after the spike reports actuals.

Q2. **Materialization target: KV entry vs new first-class derived object.**
`KVEntry` has no lineage column (`sql_models.py:1741`), so a KV target
forces lineage into `value` (a structured JSON value) or a new column. A new
derived object (a `DerivedInvariant` table carrying `source='synthesis'`,
`derived_from` JSONB, `status`) gives clean provenance and a natural
draft/active lifecycle but adds a table, a retrieval surface, and agent-tool
plumbing. Recommendation: **new first-class derived object.** It keeps R3's
"synthesis provenance, not a fabricated unit" explicit, matches the
procedural-entry precedent (a derived row with its own status), and avoids
overloading KV values with lineage. Operator decides given the
plumbing cost.

Q3. **Distillation mechanism: heuristic vs LLM.** The procedural derivation
uses DSPy LLM passes (`procedural_derivation_service.py:72`). A behavioral
invariant from a cluster of near-identical correction reasons may only need
templating/normalization. Recommendation: **heuristic first** (§10.3),
adding an LLM summarizer only if candidate quality is poor. Keeps the pass
offline-testable under `just test`.

Q4. **Where the distillation runs.** Recommendation: a standalone
leader-locked `periodic_invariant_distillation_task` in `scheduler.py`
(`:395`), NOT folded into the consolidation tick — it mines a global stream,
not per-entity dirty units, and should have its own interval/cadence.

Q5. **Test placement given the gate-collection caveat (§8).** The loop gate
`just test` collects only root `./tests/`. Recommendation: place the
loop-gating behavior test in root `./tests/` and mirror unit tests under
`packages/core/tests/`, running the latter manually. Operator may instead
choose to change the `just test` recipe to also collect
`packages/core/tests` — out of scope here, but flagged because it affects
whether the package's own suite ever gates the loop.

Q6. **Eval-suite regression check.** Should "which corrections yield which
candidate invariants" be pinned as a `memex-eval` suite
(`.claude/rules/eval-suites.md`)? Recommendation: **defer.** This feature
gates human proposals, not retrieval/extraction output; add an eval suite
only if a downstream ranking effect appears. Reassess in §10.4.
