# surprise-gated-extraction

## 1. Title

Add stage 2 of the pre-extraction admission gate: a cheap novelty
("surprise") stage that slots in BEHIND RFC #210's dedup gate at the SAME
ingestion call site, consuming its `AdmissionDecision`, and routes each
admitted note to an extraction DEPTH (full / standard-no-reflection /
deferred) instead of extract-vs-skip. It biases hard against false
negatives: no note is ever fully skipped, a downgraded note stays a full
FTS-searchable note with its raw text retained and is re-extractable later.

## 2. Size / Effort

**L.** Multiple cooperating parts, all on the hot ingestion path: new gate
config nested under #210's `AdmissionConfig` (tunable knobs), a new persisted
`Note.extraction_state` plus an alembic migration, a multi-signal novelty
scorer, a routing seam co-located with #210 at `IngestionService.ingest` that
consumes the shared `AdmissionDecision` and threads a depth into
`MemoryEngine.retain`, a new `depth` field added to the shared
`AdmissionDecision` dataclass (the verdict enum stays binary), and new
metrics. Effort is driven by (a) the false-negative safety bar — every path
must degrade toward MORE extraction, not less — (b) co-locating cleanly with
#210 behind ONE two-stage seam and ONE decision object without recomputing
the embedding #210 already computed, and (c) defining what "reduced
extraction" concretely means against a pipeline that today has one depth.
**Hard sequencing: #155 lands SECOND and depends on #210
(`pre-extraction-dedup-gate`) landing FIRST** — this ticket MODIFIES files
#210 creates (`admission/decision.py`, `AdmissionConfig`); if #210 has not
landed, those anchors do not exist yet (see §9). Decomposed into ordered
subtickets in §10; the audit/calibration loop, retroactive re-triggers, and a
universal `retain`-level guard are deferred to follow-up subtickets (§5, §10).

## 3. Triggered by

GitHub issue / RFC #155 ("Surprise-Gated Extraction Pipeline — RPE-Gated
Consolidation"), bullet #3 of the cognitive-architecture proposal in #64.
Full text: `gh issue view 155` and `/home/vscode/workspace/.temp/issues/rfc-155.md`.
Sequenced to land before #154 (hierarchical memory layers) so consolidation
does not stuff layers with low-value notes. This ticket is the SECOND stage
of a two-stage admission gate whose FIRST stage is RFC #210
(`.loop/plans/pre-extraction-dedup-gate.md`); #210's seam, emitted interface,
config surface, and the verdict/depth axis split are confirmed by the
coordinator and reconciled below. **#210 MUST land before #155** (§9).

## 4. Context

**The two-stage admission seam lives at the ingestion call site, not inside
retain (DECIDED).** RFC #210 (confirmed) adds a NEW `admission/` module
(`packages/core/src/memex_core/memory/admission/gate.py` +
`admission/decision.py`) and wires `check_admission(...)` into
`IngestionService.ingest`
(`packages/core/src/memex_core/services/ingestion.py:385`) immediately AFTER
the two-gate idempotency-check block (the `async with
self.metastore.session()` block that opens at
`packages/core/src/memex_core/services/ingestion.py:413`, skip-returns at
`:431`, and ends ~`:443`) and BEFORE the `AsyncTransaction` opens at
`packages/core/src/memex_core/services/ingestion.py:449`. That is BEFORE the
`self.memory.retain(...)` call at
`packages/core/src/memex_core/services/ingestion.py:495`. The coordinator has
DECIDED our novelty stage co-locates at the SAME call site, running on
`AdmissionDecision.verdict == PROCEED`, before the transaction opens. (My
first draft placed the gate inside `MemoryEngine.retain` at
`packages/core/src/memex_core/memory/engine.py:174`; that is reconciled. The
other-`retain`-callers coverage gap is a known limitation with a follow-up
subticket, §5 and §10.7 — no longer an open fork.)

**#210's emitted interface — consume it, do NOT recompute. Two orthogonal
axes (DECIDED).** `check_admission(...)` returns an `AdmissionDecision`
dataclass (`admission/decision.py`, an IN-PROCESS return value, NOT persisted)
with: `verdict: AdmissionVerdict` (STRICTLY binary `SKIP | PROCEED` — the
ADMISSION axis: does content enter at all), `reason`, `top_similarity: float |
None` (the nearest-neighbour normalized cosine — our novelty/distance score,
ALREADY computed), `matched_note_id: UUID | None`, `hash_hit: bool`, and
`embedding: list[float] | None` (the raw-content embedding, computed ONCE).
The verdict enum stays binary; our three extraction depths ride on a SEPARATE
new `depth` field this ticket ADDS to the `AdmissionDecision` dataclass
(the EXTRACTION-GRADATION axis, orthogonal to admission — mirroring this
codebase's `record_outcome`-vs-`deprioritize` orthogonality precedent),
consumed only when `verdict == PROCEED`. #210 already embeds the raw content
once (`generate_embedding(await get_embedding_model(), content_text)`,
`packages/core/src/memex_core/memory/extraction/embedding_processor.py:55`,
`packages/core/src/memex_core/memory/models/embedding.py:25`) and runs the
vault-scoped pgvector cosine NN (the `_detect_overlapping_notes` pattern at
`packages/core/src/memex_core/services/ingestion.py:1137`, normalized via
`get_shared_corrector().normalize(...)`,
`packages/core/src/memex_core/memory/extraction/storage.py:461`). So the
embedding-novelty signal MUST read `AdmissionDecision.top_similarity` (and
`.embedding` if it needs the vector); it must NOT re-embed or re-run the NN.

**Extraction has exactly one depth today; "reduced" must be defined.**
`ExtractionEngine.extract_and_persist`
(`packages/core/src/memex_core/memory/extraction/engine.py:319`) dispatches on
a single `active_strategy` (`page_index` or `simple`,
`packages/common/src/memex_common/config.py:812`) with one `model`
(`packages/common/src/memex_common/config.py:773`). There is no built-in
"cheap tier" — "reduced extraction" must be DEFINED by this ticket (§11 Q1).
`MemoryEngine.retain` (`packages/core/src/memex_core/memory/engine.py:174`)
calls extraction at `:209`, contradiction at `:226`, and immediate reflection
(when `reflect_after=True`) at `:254`. This ticket threads a chosen DEPTH into
`retain` so it can gate the reflection trigger and request reduced/deferred
extraction; the depth DECISION is made at the admission seam, `retain` only
APPLIES it.

**Persistence today: raw text is already retained; there is no extraction
state.** `Note` (`packages/core/src/memex_core/memory/sql_models.py:241`)
stores the full raw body in `original_text`
(`packages/core/src/memex_core/memory/sql_models.py:278`) and has a lifecycle
`status` of `active` / `superseded`
(`packages/core/src/memex_core/memory/sql_models.py:321`). There is NO
`extraction_state`. The RFC's `extraction_state = deferred` needs a NEW column
+ migration (latest is `069_nodes_chunks_search_tsvector.py`; the next is
`070`, created via `just db-revision`). #210 adds no schema change, so this
column is entirely ours. Because `original_text` already holds the raw body
and the note is a first-class FTS-searchable row, the REVERSIBLE requirement
(R3) is largely a matter of recording the state, not retaining new data.

**Config: one shared surface under `memory.admission.*` (DECIDED).** #210 adds
`AdmissionConfig` on `MemoryConfig`
(`packages/common/src/memex_common/config.py:2383`, registered next to
`extraction` at `:2410`) at key path `memory.admission.*`
(`enabled: bool = False`, `redundancy_threshold: float = 0.92`,
`near_dup_candidate_limit: int`). Our knobs are a `surprise: SurpriseConfig`
sub-model this ticket ADDS as a field on #210's existing `AdmissionConfig`
(path `memory.admission.surprise.*`), so the two-stage gate reads as ONE
config surface. `surprise.enabled` defaults OFF independently of
`admission.enabled`. Access path mirrors `self.config.server.memory.extraction`
(`packages/core/src/memex_core/api.py:445`).

## 5. Non-goals / out of scope

- **Dedup / RFC #210 is OUT of scope.** Do NOT add hash-based or exact /
  near-duplicate detection, do NOT re-embed the raw content, do NOT re-run
  the near-dup NN. #210 (hash + embedding dedup) runs FIRST and returns the
  `AdmissionDecision` this stage consumes. This ticket handles "novel-ish but
  routine" content downstream of a `PROCEED` verdict.
- **Do NOT touch the binary `AdmissionVerdict` enum.** It stays `SKIP |
  PROCEED` (the coordinator dropped #210's previously-reserved `DELTA`
  member). Our depths ride on a new `AdmissionDecision.depth` field, not on a
  new verdict (R6).
- **Universal coverage of other `retain` callers is a KNOWN LIMITATION and a
  follow-up subticket** (§10.7). The gate co-locates at the
  `IngestionService.ingest` admission seam, covering the same single-note
  surface #210 covers; batch and other `retain` callers are not gated by this
  ticket and get full extraction by default (safe, FN direction). A thin
  `retain`-level guard for universal coverage is deferred, not a fork.
- **The audit / calibration loop is DEFERRED to a follow-up subticket**
  (§10.5). This ticket lands the gate, its config, its persisted state, and
  its per-decision metrics — including a false-negative-rate metric SCAFFOLD
  — but the daily sample-and-audit job that populates that FN metric with
  ground truth is a separate subticket.
- **Retroactive re-trigger paths are DEFERRED to a follow-up subticket**
  (§10.6). This ticket persists `extraction_state = deferred`, guarantees a
  downgraded note stays FTS-searchable and re-extractable, and exposes ONE
  explicit re-extraction entry point; wiring the automatic re-triggers
  (contradiction-driven, repeated-FTS-surfacing, explicit user reference,
  periodic audit) is out of scope for the initial gate.
- Do NOT change retrieval ranking, the reflection scoring model, or the
  contradiction algorithm. The gate only chooses the depth at which those run.

## 6. Requirements & restrictions

Requirements locked by the RFC, the #210 seam, and the coordinator:

- **R1. TUNABLE (config-driven), nested under `AdmissionConfig`.** Every
  threshold and per-signal knob (a stage-2 `enabled` master switch defaulting
  OFF independently of `admission.enabled`, each signal's weight/threshold, the
  whitelist, the surprise threshold(s), the confidence-margin escalation
  threshold) MUST live in a `surprise: SurpriseConfig` sub-model this ticket
  ADDS as a field on #210's existing `AdmissionConfig`
  (`memory.admission.surprise.*`), not in a standalone config block and not
  hardcoded. Match the existing `Field(default=..., description=...)` pattern
  (`packages/common/src/memex_common/config.py:770`,
  `packages/common/src/memex_common/config.py:509`). Do NOT create
  `AdmissionConfig` — #210 owns it. Default OFF so the gate is opt-in and
  current uniform-full-extraction is unchanged.
- **R2. OBSERVABLE, `memory_admission_*` namespace with a `stage` label.**
  Surprise scores AND gate decisions (which depth each note routed to, and
  which signal(s) fired) MUST be logged and emitted as Prometheus metrics in
  `packages/core/src/memex_core/metrics.py:11` (Counter/Gauge/Histogram
  patterns; e.g. `INGESTION_TOTAL` at `:11`, `EXTRACTION_INFLIGHT` at `:232`).
  Name them `memory_admission_*`; the decision Counter
  (`memory_admission_decision_total`) carries a `stage` label (our stage emits
  `stage="surprise"`) IN ADDITION to depth + firing-signal labels, so #210's
  future SKIP decisions can land in the SAME counter without a rename. Add a
  `memory_admission_surprise_score` Histogram and scaffold a
  `memory_admission_*` false-negative-rate metric (its population is the
  deferred audit subticket, §10.5). Metric errors MUST never fail an ingest
  (follow the defensive pattern at
  `packages/core/src/memex_core/services/ingestion.py:438`).
- **R3. REVERSIBLE.** A gated-DOWN (deferred/reduced-depth) note is STILL
  stored as a full note with `original_text` retained
  (`packages/core/src/memex_core/memory/sql_models.py:278`) and MUST remain
  discoverable via note FTS. Its state MUST be recorded in a new persisted
  `Note.extraction_state`
  (`packages/core/src/memex_core/memory/sql_models.py:241`; new column +
  alembic `070`) so it can be re-extracted later. Full re-extraction of a
  deferred note MUST be reachable via one explicit entry point.
- **R4. FALSE-NEGATIVE SAFETY (the primary constraint).** Every ambiguous or
  failing path degrades toward MORE extraction. Whitelisted sources (explicit
  `/remember`, user-authored notes, decisions, calendar/meeting events) route
  to full extraction; when signals DISAGREE (wide confidence margin) the gate
  ESCALATES; any signal error, missing input, or missing upstream decision
  falls open to standard-or-full depth, never to the cheapest. No path may
  fully skip extraction. (Full SKIP is #210's verdict for redundant content;
  our stage only runs on `PROCEED`, and downstream of it "deferred" is the
  floor — never a discard.)
- **R5. CONSUME the `AdmissionDecision`, do NOT recompute.** The
  embedding-novelty signal MUST read `AdmissionDecision.top_similarity` (and
  `.embedding` when it needs the vector) in-process. It MUST run its own
  fallback compute path (embed raw text + the pgvector NN pattern at
  `packages/core/src/memex_core/services/ingestion.py:1149`) ONLY when the
  gate is invoked outside the admission seam with no decision passed. Behind
  the admission seam, re-embedding is a seam violation. `top_similarity` is
  consumed as-is for v1 (granularity note in §11 Q3).
- **R6. DEPTH rides on a new `AdmissionDecision.depth` field; the verdict enum
  stays binary.** This ticket MODIFIES #210's existing
  `admission/decision.py` to ADD a `depth` field
  (`FULL | STANDARD | DEFERRED`) to the `AdmissionDecision` dataclass,
  consumed only when `verdict == PROCEED`. Do NOT add a member to
  `AdmissionVerdict` (it stays `SKIP | PROCEED`), and do NOT create the enum
  or the dataclass — #210 ships both.

Repo principles this change must respect, each cited:

- **Every change ships a test; a behaviour proven by a test that runs in the
  gate** (`.claude/rules/python-testing.md`, constraint
  `all-code-needs-tests`). See §8.
- **Tests hitting real Postgres are `integration`-marked and excluded from
  the default run** (`.claude/rules/python-testing.md`, constraint
  `mark-and-exclude-slow-tests`; `pyproject.toml:78` sets `-m 'not
  integration'`). The migration and DB-coupled behaviour go behind
  `@pytest.mark.integration` (testcontainer Postgres); the pure scorer and
  config are offline.
- **Don't mock what you can run** (`.claude/rules/python-testing.md`,
  constraint `dont-mock-what-you-can-run`): the pure scorer is tested with
  real values; DB-backed behaviour uses testcontainer Postgres.
- **Tests are real code — no `skip` / `xfail` / `# type: ignore` to green a
  gate** (`.claude/rules/python-testing.md`, constraint `tests-are-real-code`).
- **Fix, never silence, any pre-existing failure a gate surfaces**
  (`.claude/rules/pre-existing-issues.md`).
- **Simplicity first / surgical changes** — no signal or knob beyond R1–R6;
  do not refactor the surrounding ingest, extraction, or #210 admission code
  (`CLAUDE.md` sections 2 and 3).
- **Code style**: single quotes, line length 100, async I/O, strict mypy,
  Python ≥ 3.12, dependencies via `uv add` never `pip` (`CLAUDE.md` "Code
  style"; `.claude/rules/uv-installer.md`).
- **Run the adversarial review before declaring done**
  (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

- `packages/core/src/memex_core/memory/admission/decision.py` (#210's NEW
  module) — MODIFY the existing `AdmissionDecision` dataclass to ADD a `depth`
  field (`FULL | STANDARD | DEFERRED`, defaulting to `FULL`). Do NOT add a
  member to `AdmissionVerdict` (stays binary `SKIP | PROCEED`) and do NOT
  create the enum or dataclass — they ship with #210.
- `packages/core/src/memex_core/memory/admission/surprise.py` (NEW, in #210's
  admission package) — the PURE novelty scorer + depth-decision function.
  Inputs: raw text, the optional `AdmissionDecision` (for `top_similarity` /
  `.embedding`, R5), existing-neighbor / entity / density signal inputs, and
  config. Output: a `depth` decision + which signals fired + score/margin.
  Offline-testable, no DB and no model on the pure path — this is the
  function the loop-gating test targets (§8).
- `packages/common/src/memex_common/config.py:770` — add a
  `SurpriseConfig(BaseModel)` (the R1 knobs; master switch `enabled` default
  False) and ADD it as a `surprise: SurpriseConfig` field on #210's existing
  `AdmissionConfig`, so the path is `memory.admission.surprise.*`. Do NOT
  create `AdmissionConfig`. If #210's `AdmissionConfig` is not yet merged when
  this lands, the anchor does not exist yet — see the §9 sequencing dependency.
- `packages/common/src/memex_common/config.py:2410` — no NEW top-level field;
  `admission` is already registered on `MemoryConfig` by #210. Confirm the
  nested `surprise` resolves at `memory.admission.surprise` and add
  `SurpriseConfig` to `__all__` (near
  `packages/common/src/memex_common/config.py:3052`) if separately exported.
- `packages/core/src/memex_core/memory/sql_models.py:241` — add
  `extraction_state` to `Note` (nullable Text/enum column, server-default
  `'full'`, with a `CheckConstraint` mirroring `status` at
  `packages/core/src/memex_core/memory/sql_models.py:321`; allowed values e.g.
  `full` / `standard` / `deferred`).
- `packages/core/src/memex_core/alembic/versions/070_*.py` — NEW migration
  (`just db-revision "add note extraction_state"`) adding the column with a
  server default so existing rows read as fully-extracted; add an index if the
  deferred-note re-extraction query will filter on it.
- `packages/core/src/memex_core/services/ingestion.py:443` — at the admission
  seam #210 establishes (after the idempotency block ends ~`:443`, before the
  `AsyncTransaction` opens at `:449`): when
  `self.config.server.memory.admission.surprise.enabled` AND the
  `AdmissionDecision.verdict` is `PROCEED`, invoke the surprise decision
  (consuming the decision per R5, writing its `depth`), and carry the depth to
  the `self.memory.retain(...)` call at
  `packages/core/src/memex_core/services/ingestion.py:495` (thread it as a new
  `depth` argument; default preserves today's full behaviour). Honor the
  whitelist bypass and fall-open (R4).
- `packages/core/src/memex_core/memory/engine.py:174` — `MemoryEngine.retain`
  ACCEPTS the new depth argument (default = full) and APPLIES it: thread depth
  into extraction at `packages/core/src/memex_core/memory/engine.py:209`, and
  gate the immediate reflection trigger at
  `packages/core/src/memex_core/memory/engine.py:254` to full depth only.
  Persist the resulting `extraction_state` on the note in the same
  transaction.
- `packages/core/src/memex_core/memory/extraction/engine.py:319` — accept a
  depth parameter on `extract_and_persist` (or a sibling entry) so `retain`
  can request reduced/deferred vs full; define what "reduced/deferred" runs
  (§11 Q1). Default preserves today's full behaviour.
- `packages/core/src/memex_core/metrics.py:11` — add the `memory_admission_*`
  gate metrics: `memory_admission_decision_total` Counter labeled by `stage`
  (emit `stage="surprise"`) + depth + firing signal, a
  `memory_admission_surprise_score` Histogram, and an FN-rate scaffold,
  following the existing definitions.

Test homes (every test named in §8 has its file listed here):

- `tests/test_surprise_gate_decision.py` (NEW, root `./tests/`, offline, NOT
  `integration`-marked) — the loop-gating behaviour test over the pure
  scorer/decision function in `admission/surprise.py`.
- `packages/core/tests/integration/test_int_alembic_070.py` (NEW,
  `@pytest.mark.integration`) — migration up/down against testcontainer
  Postgres, mirroring the existing per-migration tests (e.g.
  `packages/core/tests/integration/test_int_alembic_069.py`).
- `packages/core/tests/integration/test_int_surprise_gate_retain.py` (NEW,
  `@pytest.mark.integration`) — end-to-end via a real DB at the ingestion
  admission seam: a whitelisted / high-surprise note gets full extraction; a
  low-surprise note is deferred, persists `extraction_state`, is still
  FTS-searchable and re-extractable; the surprise stage consumes the
  `AdmissionDecision` without re-embedding.
- `packages/common/tests/test_config.py` (existing package config test file;
  confirm exact path in `packages/common/tests/` at implementation time) —
  add a case asserting the nested `memory.admission.surprise` defaults (master
  switch OFF) and knob parsing.

## 8. Tests & validation gates

**Eval marker (acceptance layer, REQUIRED before pickup):** `.loop/config.json`
sets `require_eval: true`, so the loop REFUSES pickup until
`.loop/evals/surprise-gated-extraction.md` exists. Author it separately with
the `create-eval` skill (see final note). It should pin, as deterministic
guardrails: no note downstream of `PROCEED` is ever fully skipped (deferred is
the floor); whitelisted sources route to full extraction; signal disagreement
escalates; a downgraded note persists `extraction_state` and stays
FTS-searchable; the gate is off by default (nested master switch); the
embedding-novelty signal CONSUMES `AdmissionDecision.top_similarity` and does
NOT re-embed when a decision is present; depth rides on
`AdmissionDecision.depth` while the verdict enum stays binary.

Gates (verified this session):

- **`just test`** = `uv run pytest tests` (`justfile:65`). Collects ONLY the
  root `./tests/` tree, and `addopts` excludes `integration`-marked tests
  (`pyproject.toml:78`, `-m 'not integration'`). Package trees
  (`packages/core/tests`, `packages/common/tests`) are NOT collected by this
  loop gate. Therefore the loop-gating behaviour test MUST live in root
  `./tests/`, be offline, and carry no `integration`/`llm` marker.
- **`just prek`** = `uv run prek run -a` (`justfile:61`) = ruff + mypy +
  format.

Tests to add:

- **Loop-gating behaviour test (offline)** —
  `tests/test_surprise_gate_decision.py`, exercising the pure decision
  function in `admission/surprise.py`. Cover with `@pytest.mark.parametrize`:
  1. A high-surprise signal set routes to `depth=FULL`; a whitelisted source
     (explicit `/remember` / user-authored / decision / calendar) ALSO routes
     to full (the bypass).
  2. A uniformly-low-surprise, non-whitelisted note routes to `depth=DEFERRED`
     — the decision object marks it stored/deferred, and verdict stays
     `PROCEED`; there is NO way to emit a skip from this stage (R4).
  3. DISAGREEING signals (wide confidence margin) ESCALATE (R4).
  4. A signal-computation error / missing input FALLS OPEN to
     standard-or-full, never the cheapest depth (R4).
  5. When an `AdmissionDecision` with a `top_similarity` is PASSED IN, the
     embedding signal consumes it and does NOT invoke the fallback compute
     path; when no decision is passed, the fallback path is used (R5). Assert
     the fallback was not called using `assert mock.method_calls == []` (per
     `.claude/rules/python-testing.md`, "Writing good tests"), not
     `assert_not_called()`.
  6. With the nested master switch OFF, the stage is a no-op and the note
     takes the current full path (default-unchanged behaviour).
- **Migration test (integration)** —
  `packages/core/tests/integration/test_int_alembic_070.py`: upgrade adds
  `extraction_state` with the server default so a pre-existing row reads as
  fully-extracted; downgrade removes it. Real testcontainer Postgres.
- **Retain end-to-end (integration)** —
  `packages/core/tests/integration/test_int_surprise_gate_retain.py`: with the
  stage enabled, a low-surprise note is deferred, persists `extraction_state`,
  is returned by note FTS, and can be re-extracted via the explicit entry
  point; a whitelisted/high-surprise note gets the full pipeline; the stage
  reads the `AdmissionDecision` embedding rather than re-embedding. Real DB;
  no mocks on the data layer.
- **Config defaults (package unit, manual)** — in `packages/common/tests/`
  assert the nested `memory.admission.surprise` defaults (master OFF) and knob
  parsing.

Both `just test` and `just prek` must pass before done. Run the adversarial
review (`.claude/rules/adversarial-reviews.md`) after gates are green.

## 9. Risk assessment

- **HARD cross-ticket dependency — #210 must land first.** This ticket
  MODIFIES files #210 creates: `admission/decision.py` (adds the `depth` field
  to `AdmissionDecision`) and `AdmissionConfig`
  (`packages/common/src/memex_common/config.py`, #210 adds it at the
  `MemoryConfig` class ~`:2383`). If #210 has not landed, those anchors do not
  exist and this ticket cannot be implemented as written. The loop MUST
  sequence #210 (`pre-extraction-dedup-gate`) before #155. Coordinate any
  rename of the shared `AdmissionDecision`/`AdmissionConfig` symbols with #210.
- **Blast radius: HIGH — hot ingestion path, shared with #210.** The stage
  sits at the `IngestionService.ingest` admission seam (~`:443`,
  `packages/core/src/memex_core/services/ingestion.py:385`), which every
  single-note ingest funnels through, and threads a depth into
  `MemoryEngine.retain` (`packages/core/src/memex_core/memory/engine.py:174`).
  A wrong downgrade silently under-extracts a note. Mitigation: the nested
  master switch defaults OFF (R1); the fall-open rules (R4) route ambiguity to
  more extraction; the offline test pins the never-skip and escalate-on-
  disagreement invariants.
- **Reversibility: HIGH at the data level, per-note recoverable.** No memory
  is destroyed — `original_text` is retained
  (`packages/core/src/memex_core/memory/sql_models.py:278`), a downgraded note
  stays a full FTS row (R3), re-extractable via the explicit entry point. The
  feature reverts by flipping the switch off; the migration is a plain
  additive column (070) with a safe server default.
- **Likeliest failure modes:**
  1. **False negative (the RFC's core fear)** — a routine-looking note that
     held the one important decision gets deferred. Countered by whitelist
     bypass, escalate-on-disagreement, fall-open, retained raw text, and the
     deferred re-extraction path.
  2. **Re-embedding the content #210 already embedded** — a concrete seam
     violation: #210 hands us `AdmissionDecision.embedding` and
     `.top_similarity`, computed once. The embedding-novelty signal MUST read
     those (R5); the fallback compute path runs only outside the admission
     seam. Guarded by test case 5.
  3. **Putting depth on the wrong axis** — adding a verdict member or creating
     the enum/dataclass ourselves instead of adding the `AdmissionDecision.depth`
     field (R6) would split the shared contract. Guarded by R6 and coordinated
     with #210.
  4. **"Reduced" depth undefined against a one-depth pipeline** — resolve §11
     Q1 before subticket 3, or "reduced" collapses to full-or-skip.
  5. **Migration drift** — adding `extraction_state` without a server default
     breaks existing rows / the idempotency read at `:424`. Countered by the
     070 default and the migration integration test.
  6. **Coverage gap vs `retain`'s other callers** — a known limitation (§5),
     not a defect: the seam covers the same single-note surface #210 covers;
     other `retain` callers get full extraction by default (safe, FN
     direction). Universal coverage is the follow-up subticket §10.7.
  7. **Metrics blocking ingest** — a metrics error must never fail an ingest;
     follow the defensive pattern at
     `packages/core/src/memex_core/services/ingestion.py:438`.

## 10. Subtickets

Dependency-ordered (all gated behind #210 having landed — §9):

1. **Config (nested) + shared `depth` field + schema + state (foundation).**
   Add `SurpriseConfig` and attach it as `surprise` on #210's `AdmissionConfig`
   (`packages/common/src/memex_common/config.py:770`, path
   `memory.admission.surprise.*`); ADD the `depth` field to #210's
   `AdmissionDecision` dataclass in `admission/decision.py` (R6, leave the
   verdict enum binary); add `Note.extraction_state`
   (`packages/core/src/memex_core/memory/sql_models.py:241`) and the alembic
   `070` migration. Verify: config-defaults test + migration integration test
   green; `just prek` (mypy) green. No behaviour change yet (switch OFF).
2. **Novelty signal computation (pure).** Build `admission/surprise.py`: the
   signal panel — embedding-novelty CONSUMING `AdmissionDecision.top_similarity`
   with a fallback compute path (R5); entity-novelty via
   `EntityResolver.get_entity_by_text`
   (`packages/core/src/memex_core/memory/entity_resolver.py:717`);
   information-density heuristic; structural/whitelist — the OR-combine +
   confidence-margin score, and the pure depth-decision function returning a
   `depth`. Verify: the offline loop-gating test
   (`tests/test_surprise_gate_decision.py`) green under `just test`. Note
   whether the NLI-pre-gate signal is included or deferred (§11 Q2).
3. **Gate routing at the admission seam.** Wire the decision into
   `IngestionService.ingest` at the seam #210 establishes (~`:443`, before the
   transaction at `:449`), on `PROCEED`; thread the chosen depth into
   `self.memory.retain(...)` at
   `packages/core/src/memex_core/services/ingestion.py:495`; have `retain`
   (`packages/core/src/memex_core/memory/engine.py:174`) apply the depth to
   `extract_and_persist` (`:209`) and gate reflection (`:254`), and persist
   `extraction_state`. All behind the master switch, honoring whitelist bypass
   and fall-open. Verify: the retain integration test green. Depends on §11 Q1
   (what "reduced/deferred" runs).
4. **Metrics / observability.** Add `memory_admission_decision_total`
   (labeled by `stage="surprise"` + depth + firing signal), the
   `memory_admission_surprise_score` Histogram, and the FN-rate scaffold in
   `packages/core/src/memex_core/metrics.py:11`; add structured logging of each
   decision. Verify: metrics assertions in the retain integration test.
5. **(Follow-up) Audit / calibration loop + FN metric population.** The daily
   sample-and-audit job that runs full extraction on a random N% of deferred
   notes, compares yield, and populates the FN-rate metric. Deferred (§5).
6. **(Follow-up) Retroactive re-trigger paths.** Automatic re-extraction of
   `deferred` notes on contradiction, repeated FTS surfacing without units,
   explicit user reference, and periodic audit. Deferred (§5); this ticket
   only guarantees the explicit re-extraction entry point exists.
7. **(Follow-up) Universal `retain`-level guard.** A thin depth guard at
   `MemoryEngine.retain` so batch and other non-ingestion `retain` callers are
   also gated, closing the coverage gap (§5, §9 failure mode 6). Deferred; the
   initial gate covers the single-note ingest path only.

Order rationale: config + the shared `depth` field + schema must exist before
anything reads a knob, sets a depth, or writes a state; the pure scorer (2) is
the loop gate; routing (3) consumes the seam and the scorer; metrics (4)
observe the routed decisions; audit (5), re-triggers (6), and universal
coverage (7) build on the persisted `deferred` state and the seam.

## 11. Open questions

Two forks remain for the operator; the seam, the verdict/depth axis split, the
config surface, the metric labels, coverage, and #210 sequencing are DECIDED
(§4, §5, §6, §9) and are not forks.

- **Q1 — what "reduced / deferred extraction" concretely means, given a
  one-depth pipeline.** `extract_and_persist`
  (`packages/core/src/memex_core/memory/extraction/engine.py:319`) has a single
  depth; `ExtractionConfig` exposes one `model`
  (`packages/common/src/memex_common/config.py:773`) and a `simple` vs
  `page_index` strategy (`:812`). The RFC's "low → cheap extraction (small
  model, single pass)" does not exist yet. **Recommendation:** define the three
  `depth` values as FULL = today's pipeline + reflection trigger; STANDARD =
  today's pipeline, NO reflection trigger (gate
  `packages/core/src/memex_core/memory/engine.py:254` off); DEFERRED = persist
  the full note with `extraction_state = deferred` and `original_text`
  retained, running no LLM extraction now, recoverable via the explicit
  re-extraction entry point. This keeps the FN bar safe (nothing lost, only
  postponed) and avoids inventing a half-built small-model extractor here; a
  genuine cheap-model tier can be a later subticket if measured savings justify
  it. Confirm the three-way mapping with the operator before subticket 3.
- **Q2 — signal-panel scope: which signals ship in the first gate.** The
  embedding-novelty (consuming `top_similarity`), entity-novelty
  (`EntityResolver.get_entity_by_text`,
  `packages/core/src/memex_core/memory/entity_resolver.py:717`),
  information-density, and structural/whitelist signals are all buildable. The
  NLI-vs-top-K signal is NOT reusable as-is:
  `ContradictionEngine.detect_contradictions`
  (`packages/core/src/memex_core/memory/contradiction/engine.py:70`) runs on
  post-extraction `unit_ids`, not on incoming raw text. **Recommendation:**
  ship the first four signals in subticket 2 and DEFER the NLI-pre-gate signal
  (it needs a new raw-text-vs-existing-units NLI path). Because signals are
  OR-combined and the gate escalates on disagreement, omitting NLI initially
  only makes the gate MORE conservative (fewer downgrades) — the safe direction
  for the FN bar. Operator to confirm NLI can be a follow-up.

Decided-with-default (recorded, not forks; flagged for operator visibility):

- **Audit/calibration loop deferred** to subticket 5 — the FN-recovery
  guarantee comes from reversibility (retained raw text + deferred state), not
  calibration, so the gate ships safely without it. This ticket lands only the
  FN-rate metric scaffold (R2).
- **Embedding granularity consumed as-is (v1).** Consuming
  `AdmissionDecision.top_similarity` inherits #210's approximation (for long
  content, a truncated/leading-window content embedding vs passage-level chunk
  embeddings, #210 Q1). This is tolerable because both stages fail open toward
  MORE processing — #210 PROCEEDs on lookup error (never falsely skips) and our
  gate escalates on weak/degraded signal. If routing later proves sensitive,
  negotiate a joint embedding-granularity contract with #210 rather than
  re-embedding here.
