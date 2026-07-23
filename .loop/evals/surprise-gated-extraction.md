eval: surprise-gated-extraction

**Definition of Done:** a cheap novelty ("surprise") gate at the shared admission
seam routes each admitted note to an extraction DEPTH (full / standard / deferred)
instead of extract-vs-skip, biasing hard against false negatives — no note is ever
fully skipped, a downgraded note stays a full FTS-searchable note that is
re-extractable later — while staying off by default, tunable, observable, and
consuming #210's already-computed novelty signal rather than re-embedding.

Scoring policy: every row is a deterministic assertion on the routed decision /
persisted state / emitted metric at a hard 100% bar. Rows 1–7 are the load-bearing
guardrails (never-skip, whitelist bypass, escalate-on-disagreement, fall-open,
off-by-default, reversible-and-searchable, consume-not-recompute); a hole in any one
of them regresses the false-negative bar the whole feature exists to protect, so they
must pass 100%.

Fork-dependent rows are marked. They are written against the planner's recommended
resolutions (ticket §11) and must be re-pinned if the operator decides otherwise.
Numbering matches the ticket's final §11 (the #210-seam question is no longer a fork
— it is DECIDED, see below):
- **Ticket §11 Q1 (what "reduced / deferred" means, given today's one-depth pipeline)**
  → rows 8–10 assume the three-way mapping **FULL = today's pipeline + reflection
  trigger; STANDARD = today's pipeline, NO reflection trigger; DEFERRED = persist the
  full note, `extraction_state = deferred`, run no LLM extraction now, recover later**.
  If the operator wants a genuine cheap-model tier instead of DEFERRED, re-pin rows 9–10.
- **Ticket §11 Q2 (signal panel)** → the gate ships the four buildable signals
  (embedding-novelty, entity-novelty, information-density, structural/whitelist); the
  NLI-pre-gate signal is deferred. Rows below do not depend on NLI.
- **#210 seam — DECIDED (not a fork).** Row 7 is written against #210's CONFIRMED
  interface: the gate sits at `IngestionService.ingest` consuming an `AdmissionDecision`
  whose `top_similarity` is the raw-content nearest-neighbour cosine #210 already
  computed. Representation is pinned (coordinator + #210): `AdmissionVerdict` stays
  binary `SKIP | PROCEED`; our three depths ride on a separate `depth` field on
  `AdmissionDecision`, consumed only when `verdict == PROCEED`. This ticket runs second
  and EXTENDS #210's `admission/decision.py` (adds the `depth` field) rather than
  creating the enum/dataclass.

Ticket: `.loop/plans/surprise-gated-extraction.md`.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL — never skip]** A uniformly-low-surprise, non-whitelisted note is still stored as a full, searchable note; only extraction is reduced | Gate enabled; ingest a routine restatement note whose every signal is low (e.g. "Reminder: the standup is still at 9am, same as always."), with no upstream contradiction | The note row persists with `extraction_state` recorded as a reduced/deferred value; the decision object never emits a "skip"/drop outcome (no such output exists) | Deterministic: assert note persisted AND `note.extraction_state in {'reduced','deferred'}` AND decision has no skip/drop value | 100% |
| **[GUARDRAIL — whitelist bypass]** A whitelisted source routes to FULL extraction regardless of low signals | Gate enabled; ingest a low-surprise body but via an explicit `/remember` / user-authored / decision / calendar source | Gate is bypassed to full depth; the note is fully extracted and its `extraction_state` is the full value; the reflection trigger fires | Deterministic: assert decision depth == FULL AND `note.extraction_state == 'full'` AND memory units were produced | 100% |
| **[GUARDRAIL — escalate on disagreement]** When signals disagree (wide confidence margin) the gate escalates, never downgrades | Gate enabled; a note where one signal reads high-novelty and another reads low (deliberate disagreement) | The decision routes to full-or-standard (escalated), NOT to the reduced/deferred depth | Deterministic: assert decision depth in {FULL, STANDARD} AND depth != REDUCED | 100% |
| **[GUARDRAIL — fall-open]** A signal-computation error or missing input degrades toward MORE extraction, never the cheapest depth | Gate enabled; force one signal computation to raise (e.g. the embedding backend errors) and no upstream `top_similarity` present | The gate falls open to standard-or-full depth; it does NOT route to REDUCED/deferred, and the ingest itself does not fail | Deterministic: assert no exception propagates AND decision depth in {FULL, STANDARD} | 100% |
| **[GUARDRAIL — off by default]** With the master switch off, the gate is a no-op and every note takes today's full path | `memory.admission.surprise.enabled = False` (default); ingest any note | The note is fully extracted exactly as today; `extraction_state` is the full value; no downgrade path runs | Deterministic: assert decision depth == FULL (or gate not invoked) AND `note.extraction_state == 'full'` | 100% |
| **[GUARDRAIL — reversible & searchable]** A downgraded/deferred note remains discoverable by note FTS and is re-extractable via the explicit entry point | Gate enabled; ingest a low-surprise note so it defers; then (a) run note FTS for a term in its body, (b) invoke the explicit re-extraction entry point | (a) the deferred note is returned by note search; (b) after re-extraction its `extraction_state` becomes full and memory units now exist | Deterministic: assert deferred note in FTS results AND, post re-extract, `note.extraction_state == 'full'` AND unit_count > 0 | 100% |
| **[GUARDRAIL — consume, not recompute]** When an upstream novelty signal is present the gate reads it and does NOT re-embed/re-search | Gate enabled; call the admission path with an `AdmissionDecision` carrying `top_similarity` (and `embedding`); spy on the embedding backend + NN query | The embedding-novelty signal reads `top_similarity`; the fallback embed+pgvector-NN path is not invoked | Deterministic: assert the fallback embed/NN spy recorded no calls (`assert spy.method_calls == []`) AND the decision used the supplied similarity | 100% |
| High-surprise note routes to FULL depth and triggers reflection *(Q2-dependent: FULL)* | Gate enabled; ingest a genuinely novel note (new entities, high info-density, far from any neighbour: e.g. a first-ever decision record about a new vendor) | Decision depth == FULL; full pipeline runs and the reflection trigger fires | Deterministic: assert depth == FULL AND reflection enqueued/triggered | 100% |
| Medium-surprise note routes to STANDARD depth: extracted but no reflection trigger *(Q2-dependent: STANDARD = no-reflection)* | Gate enabled; ingest a moderately novel note (some new facts, near an existing neighbour but not a restatement) | Decision depth == STANDARD; memory units are produced; the reflection trigger does NOT fire | Deterministic: assert depth == STANDARD AND unit_count > 0 AND reflection NOT triggered | 100% |
| Low-surprise note routes to REDUCED == DEFERRED: no LLM extraction now *(Q2-dependent: REDUCED = DEFERRED)* | Gate enabled; ingest a low-surprise restatement (same note body as row 1) | Decision depth == REDUCED; `extraction_state == 'deferred'`; NO memory units created at ingest; no extraction LLM call made | Deterministic: assert depth == REDUCED AND `note.extraction_state == 'deferred'` AND unit_count == 0 AND no extraction LLM call | 100% |
| **[OBSERVABLE]** Every routed decision emits an observability signal labeled by stage, depth, and firing signal | Gate enabled; ingest one note that routes to each of FULL / STANDARD / DEFERRED | `memory_admission_decision_total` is incremented once per note with `stage="surprise"` plus the chosen depth and firing-signal labels (the `stage` label lets #210's future SKIP decisions share the counter); a surprise-score value is recorded | Deterministic: assert the decision counter incremented with `stage="surprise"` + expected depth+signal labels AND a score sample recorded | 100% |
