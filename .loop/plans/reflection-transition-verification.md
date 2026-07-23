# reflection-transition-verification: flag omission/corruption/hallucination in the reflection Phase-4 consolidation transition

## 1. Title

Add a deterministic **transition verifier** that scores the reflection
Phase-4 consolidation transition (source observations + validated new
observations → merged output observations + entity summary) on
**coverage** (was source evidence dropped) and **faithfulness** (does
the output cite evidence that never existed in the source), and
**records** sub-threshold transitions via a structured log line plus a
Prometheus counter — **without blocking** the Phase-5 write. This is
the cheap, deterministic first slice of RFC #258; the LLM-entailment
**preservation** (corruption) check and any persisted `transition_log`
table are explicitly deferred (see §5, §11).

## 2. Size / Effort

**M.** One new pure module (`transition_verifier.py`), a Pydantic
verdict model, three config fields, two Prometheus counters, and one
call site wired into `_reflect_entity_internal` between Phase 4 and
Phase 5. The verifier itself is a pure, offline, deterministic function
over two in-memory `list[Observation]` — the bulk of the effort is its
unit tests and getting the coverage/faithfulness definitions right
against real Phase-4 evidence semantics, not the wiring. No new DB
table, no migration, no LLM call in this slice.

## 3. Triggered by

RFC #258 (`/home/vscode/workspace/.temp/issues/rfc-258.md`, `gh issue
view 258`): the consolidation/reflection pipeline trusts LLM output —
if a merged mental model is generated, it is stored. TrustMem
(arXiv:2606.25161) shows three transition-level failures go undetected:
**omission** (source info dropped), **corruption** (meaning altered),
**hallucination** (unsupported content invented). Checking the target
state in isolation misses these because a bad transition can produce a
valid-looking target. Mental models feed session briefings and surveys,
so a silent bad consolidation poisons everything downstream and is
currently undetectable.

## 4. Context (today's state, cited)

### The transition to verify

Reflection is the Hindsight loop, Phases 0–6, orchestrated in
`_reflect_entity_internal`
(`packages/core/src/memex_core/memory/reflect/reflection.py:509`). The
single **synthesis/consolidation** step is **Phase 4 — Compare**:

- `reflection.py:1884` — `_phase_4_compare(existing, new_obs, ...) ->
  tuple[list[Observation], str]`. It merges prior observations
  (`existing`) with validated new ones (`new_obs`) via an LLM
  (`ComparePhaseSignature`, LLM call at `reflection.py:1982`,
  `operation_name='reflection.compare'`) and returns
  `(final_list, entity_summary)` (`reflection.py:2090`).
- Call site: `reflection.py:606-611` — `final_obs, entity_summary =
  await self._phase_4_compare(updated_observations, validated, ...)`.
  Here **`updated_observations`** (prior observations after Phase 0)
  and **`validated`** (Phase-3 validated new observations) are the
  transition **source**; **`final_obs`** + **`entity_summary`** are the
  transition **target**. Both source and target are fully in memory at
  this seam, before any persist.

### Where the target is persisted (the point we gate *before*)

- `reflection.py:626-635` — Phase 5 runs immediately after Phase 4:
  `_phase_5_finalize(mental_model, final_obs, ..., entity_summary=...)`.
- `_phase_5_finalize` (`reflection.py:661`) issues a version-checked CAS
  `UPDATE mental_models` (`reflection.py:720-737`) writing
  `observations` (JSONB), `entity_metadata.description` (the summary),
  bumped `version`, and a fresh embedding, then commits
  (`reflection.py:756`). Table `MentalModel` /`mental_models`
  (`packages/core/src/memex_core/memory/sql_models.py:138`);
  `observations` is a JSONB list of serialized `Observation`
  (`sql_models.py:156`).

### Observation shape (the unit of comparison)

- `Observation(BaseModel)` — `sql_models.py:123`: `id: UUID`
  (`:126`), `title` (`:127`), `content` (`:128`), `trend` (`:129`),
  `evidence: list[EvidenceItem]` (`:133`).
- `EvidenceItem(BaseModel)` — `sql_models.py:107`: `memory_id: UUID`
  (`:110`), `quote` (`:111`), `relevance` (`:112`), `explanation`
  (`:115`), `timestamp` (`:118`).
- **The deterministic anchor:** every observation carries a set of
  evidence `memory_id` UUIDs. The **source evidence universe** = the
  union of `memory_id`s across `existing` + `new_obs`; the **target
  evidence set** = the union across `final_obs`. Coverage and
  faithfulness are set relations over these UUIDs — no text semantics
  required.

### What exists today for provenance / observability (partial, not a verifier)

- Phase 4 already reconstructs evidence UUIDs from LLM integer indices
  via `int_to_uuid` and **logs a warning on an out-of-bounds index**
  (`reflection.py:2049`, `:2051`) but does **not** treat it as a
  transition failure or meter it as hallucinated provenance.
- `ComparePhaseOutput.provenance`
  (`packages/core/src/memex_core/memory/reflect/prompts.py:190`,
  `:217`) carries per-output `status: Literal['added','merged','kept']`
  and `merged_from_existing_indices` — a merge/lineage signal, metered
  only for malformation via `PHASE4_PROVENANCE_MALFORMED_TOTAL`
  (`metrics.py:149`; incremented at `reflection.py:2009` etc.). It is
  **not** a coverage/faithfulness check.
- Logging in reflection is stdlib `logging`
  (`reflection.py:82`), not structlog. Metrics are Prometheus
  `Counter`s in `packages/core/src/memex_core/metrics.py` (pattern:
  `REFLECTION_CAS_ABANDONS_TOTAL` at `metrics.py:63`; labelled counter
  `PHASE4_PROVENANCE_MALFORMED_TOTAL` at `metrics.py:149`).

### Reusable comparison machinery (for the *deferred* LLM check only)

- Contradiction detection already has an LLM entailment-style primitive
  — `ClassifyRelationships`
  (`packages/core/src/memex_core/memory/contradiction/signatures.py:62`,
  invoked in `ContradictionEngine._classify`,
  `memory/contradiction/engine.py:394`) returning
  `reinforce|weaken|contradict` with `authoritative` + `reasoning`. If
  a follow-up adds the LLM **preservation** check, this is the pattern
  to reuse (via `run_dspy_operation`, `llm.py:56`). **Out of scope for
  this slice** (§5).

### The RFC's proposed surface vs. this slice

RFC #258 sketches `memory/transition_verifier.py`,
`memory/transition_log.py`, `memory/quality_gate.py`, plus edits to
`services/consolidation.py`, `services/lint.py`,
`services/reflection.py`, and `memory/confidence.py`, and a
preference-pair optimisation phase. **None of those files exist**
(confirmed: no `transition`, `quality_gate`, or `transition_log` module
under `memory/`). This ticket delivers **only**
`memory/transition_verifier.py` + its wiring at the Phase-4 seam. The
log/gate/confidence/optimisation pieces are deferred (§5, §11 Q1).

### Config / gates

- Reflection config: `ReflectionConfig`
  (`packages/common/src/memex_common/config.py:509`), already read in
  the engine as `self.config.server.memory.reflection.*` (e.g.
  `enrichment_enabled` at `reflection.py:650`). New fields land here.
- **Loop test gate `just test` → `uv run pytest tests`** (`justfile:65`)
  collects the **root `./tests/` suite ONLY**;
  `packages/core/tests/**` is **not** collected by the loop gate.
  `pyproject.toml:78` `addopts` runs `-m 'not integration'`. So the
  DoD-bearing verifier tests MUST live under root `./tests/` and be
  pure/offline (no Docker, no LLM) to run in the loop.
- **Lint/type gate `just prek` → `uv run prek run -a`** (`justfile:61`):
  ruff + mypy strict, single quotes, line length 100, Python ≥ 3.12.

## 5. Non-goals / out of scope

- **No LLM entailment / preservation (corruption) check in this slice.**
  Deterministic text comparison cannot reliably detect meaning change
  of a retained claim. The preservation dimension is deferred to a
  follow-up that reuses `ClassifyRelationships`
  (`contradiction/signatures.py:62`). This slice computes coverage +
  faithfulness only. (§11 Q2.)
- **No new DB table / no `transition_log.py` / no alembic migration.**
  Recording is via structured log + Prometheus counter only. Persisting
  transition records is a follow-up. (§11 Q1.)
- **No `quality_gate.py`, no hold-for-review queue, no auto-reject.**
  "Gate, don't block": Phase 5 still runs unconditionally. A hard-fail
  mode is investigated in §11 Q3 but NOT implemented here.
- **No changes to `services/consolidation.py`.** Its
  only-direct-write-is-the-tick-row invariant is enforced by
  `packages/core/tests/unit/test_consolidation_writes.py`; verification
  hooks at the reflection Phase-4 seam, not in the consolidation
  orchestrator. Do not add verification to `tick()`.
- **No edits to `memory/confidence.py`.** "Transition quality feeds
  confidence" (RFC Impact) is a later phase; this slice does not adjust
  any unit/model confidence.
- **No changes to `services/lint.py`** (RFC's "lint gains a
  transition-verification dimension" is out of scope).
- **No preference-pair construction / model fine-tuning** (RFC step 4).
- **No change to the summary-faithfulness path** beyond what evidence-set
  coverage/faithfulness already captures — `entity_summary` is free text
  with no evidence ids, so its faithfulness is an LLM-check concern (§11
  Q2), not deterministic here.
- Do not touch the `_refresh_observation` single-observation path
  (`reflection.py:765`) in this slice; it is a candidate second call
  site noted for a follow-up (§11 Q4), not wired here.

## 6. Requirements & restrictions

**Must achieve:**

- **R1 — Pure verifier module.** Add
  `packages/core/src/memex_core/memory/transition_verifier.py`
  exposing a pure, synchronous, dependency-free (no DB, no LLM, no
  network) function, e.g.
  `verify_reflection_transition(source: list[Observation], target:
  list[Observation], *, coverage_min: float, faithfulness_min: float)
  -> TransitionVerdict`. `Observation` imported from
  `memory.sql_models`.
- **R2 — Coverage (omission signal).** `coverage = |src_evidence ∩
  tgt_evidence| / |src_evidence|` over evidence `memory_id` UUID sets,
  where `src_evidence` = union of `memory_id` across `source`,
  `tgt_evidence` = union across `target`. Define `coverage = 1.0` when
  `src_evidence` is empty (nothing to retain → no omission). Flag when
  `coverage < coverage_min`.
- **R3 — Faithfulness (hallucination signal).** `faithfulness =
  |tgt_evidence ∩ src_evidence| / |tgt_evidence|` — the fraction of
  target evidence ids that actually existed in the source universe. A
  target observation citing a `memory_id` absent from the source
  universe is fabricated provenance. Define `faithfulness = 1.0` when
  `tgt_evidence` is empty. Flag when `faithfulness < faithfulness_min`.
- **R4 — Verdict model.** A Pydantic `TransitionVerdict` carrying
  `coverage: float`, `faithfulness: float`, `flags: list[str]` (subset
  of `{'omission','hallucination'}`), and the raw counts needed for the
  log line (e.g. `source_evidence_count`, `target_evidence_count`,
  `dropped_evidence_count`, `unsupported_evidence_count`). `preservation`
  MAY be present as `None`/omitted to leave room for the deferred LLM
  check — document it as not-yet-computed, do not fabricate a score.
- **R5 — Wiring, record-only.** Call the verifier in
  `_reflect_entity_internal` immediately after Phase 4
  (`reflection.py:611`, i.e. after `final_obs, entity_summary` are
  bound and before the `if not entity_summary` block at `:621`), gated
  on a config flag (R7). On any flag: emit **one structured log line**
  (stdlib `logger.warning('reflection.transition.flagged', extra={...})`
  with `entity_id`, `vault_id`, `coverage`, `faithfulness`, `flags`,
  and the counts) and **increment a Prometheus counter per flagged
  dimension**. Phase 5 MUST still run unconditionally — the verdict
  never short-circuits the write (RFC "gate, don't block").
- **R6 — Metrics.** Add to `metrics.py` a labelled counter
  `REFLECTION_TRANSITION_FLAGGED_TOTAL` (label `dimension` ∈
  `{'omission','hallucination'}`), following the
  `PHASE4_PROVENANCE_MALFORMED_TOTAL` pattern (`metrics.py:149`).
- **R7 — Config, default-safe.** Add to `ReflectionConfig`
  (`config.py:509`): `transition_verification_enabled: bool` (default
  chosen per §11 Q5), `transition_coverage_min: float`
  (0 ≤ x ≤ 1, default e.g. 0.9), `transition_faithfulness_min: float`
  (0 ≤ x ≤ 1, default e.g. 1.0 — any unsupported evidence id flags).
  When disabled, the verifier is not called and reflection behaviour is
  byte-for-byte unchanged.

**Restrictions (repo principles, cited):**

- **`.claude/rules/python-testing.md`** (`all-code-needs-tests`,
  `tests-are-real-code`, `mark-and-exclude-slow-tests`): the verifier
  ships with tests that run under the loop gate; they are typed and
  pass `just prek`; no `skip`/`xfail`/`# type: ignore` to go green. The
  pure function is exercised for real (it needs no mocks). Because
  `just test` collects only root `./tests/`, the gating tests live
  there (§8).
- **`.claude/rules/pre-existing-issues.md`**: if wiring surfaces an
  adjacent defect (e.g. the Phase-4 out-of-bounds-index warning at
  `reflection.py:2049` overlaps the faithfulness signal), record it —
  do not silently rework it, and do not scope-creep a fix.
- **`.claude/rules/adversarial-reviews.md`**: run an adversarial review
  (loop `adversarial` pass is enabled in `.loop/config.json`) before
  reporting done; act on confirmed findings.
- **Surgical changes** (CLAUDE.md §3): every changed line traces to
  this ticket. Do not "improve" adjacent reflection code, the Phase-4
  provenance handling, or the CAS logic.
- **Simplicity first** (CLAUDE.md §2): no speculative abstraction for
  the deferred LLM/persist/gate phases — a `preservation`
  placeholder is the only forward hook, and it stays inert.
- **Style** (CLAUDE.md): single quotes, line length 100, strict mypy,
  all new I/O-touching code async — but the verifier is deliberately
  pure/sync (it does no I/O), which mypy will confirm.

## 7. Code surface (files to touch; anchors to re-open)

- **`packages/core/src/memex_core/memory/transition_verifier.py`** —
  **NEW.** Pure module: `TransitionVerdict` Pydantic model +
  `verify_reflection_transition(...)`. Imports `Observation` from
  `..sql_models`. No async, no DB, no LLM.
- **`packages/core/src/memex_core/memory/reflect/reflection.py`** —
  wire the call at the Phase-4 seam. Read/insert after
  `reflection.py:611` (the `_phase_4_compare` return binding) and
  before `:621`. Add the import + `logger.warning('reflection.transition.flagged', ...)`
  + counter increments, gated on the config flag. Do NOT alter Phase 5
  (`:626-659`) control flow.
- **`packages/core/src/memex_core/metrics.py`** — add
  `REFLECTION_TRANSITION_FLAGGED_TOTAL` labelled counter near the other
  reflection counters (`metrics.py:63`, `:149`).
- **`packages/common/src/memex_common/config.py`** — add the three
  fields to `ReflectionConfig` (`config.py:509-561` block, alongside
  `enrichment_enabled` at `:548`).
- **`tests/test_transition_verifier.py`** — **NEW, root suite** (so
  `just test` collects it). Pure/offline unit tests of the verifier
  (see §8). This is the DoD-gating test file.
- **`packages/core/tests/unit/memory/reflect/`** — **NEW (optional,
  non-gating):** a unit test that the Phase-4 seam invokes the verifier
  and logs/meters on a flagged transition, using a mocked engine in the
  style of `packages/core/tests/unit/test_consolidation_writes.py`
  (source-inspection) or a constructed engine with patched phases.
  Lives here (not root) because it imports core internals; note it does
  NOT run under the loop gate (§8, §11 Q6).

## 8. Tests & validation gates

**Gates (both must pass at close):** `just test` (`uv run pytest
tests`, root suite only) and `just prek` (`uv run prek run -a`, ruff +
mypy strict).

**Eval marker (acceptance spec):** `.loop/evals/reflection-transition-verification.md`
(validated `valid` via `loopctl eval`). Seven scored scenarios — faithful
no-flag, omission flag, hallucination flag, independent dual-flag,
empty-set edge, and two deterministic guardrails (a flagged transition
still persists / disabled flag is a no-op). Keep this list and §8 in step.

**Gating tests — `tests/test_transition_verifier.py` (pure, offline):**
Construct `Observation`/`EvidenceItem` instances directly
(`memex_core.memory.sql_models`), no DB. Use fixed UUIDs for
determinism (no `uuid4()` where the assertion depends on identity;
`.claude/rules/python-testing.md` → deterministic). Cover, ideally via
`@pytest.mark.parametrize`:

1. **Clean transition** — target retains all source evidence ids and
   cites none outside the source universe → `coverage == 1.0`,
   `faithfulness == 1.0`, `flags == []`.
2. **Omission** — target drops a source evidence id (e.g. 1 of 4
   retained) → `coverage == 0.75 < coverage_min`, `'omission' in
   flags`, `faithfulness == 1.0`.
3. **Hallucination** — target cites a `memory_id` absent from the
   source universe → `faithfulness < 1.0`, `'hallucination' in flags`,
   `coverage` unaffected. (Mirrors the real Phase-4 out-of-bounds-index
   case at `reflection.py:2049`.)
4. **Independence** — a transition that both drops a source id AND adds
   an unsupported id flags **both** dimensions (coverage and
   faithfulness are independent, per RFC "three dimensions are
   independent").
5. **Empty-set edges** — empty source → `coverage == 1.0` (no
   omission); empty target evidence → `faithfulness == 1.0` (nothing to
   fabricate); both empty → no flags. Guards the divide-by-zero.
6. **Threshold boundary** — `coverage == coverage_min` does NOT flag
   (strict `<`), `coverage_min - ε` does. Pins the comparator.

All cases pure, offline, fully typed, no `skip`/`xfail`.

**Non-gating wiring test (optional, `packages/core/tests/unit/...`):**
assert the Phase-4 seam calls the verifier and, on a flagged verdict,
increments `REFLECTION_TRANSITION_FLAGGED_TOTAL` and does NOT skip
Phase 5. Because the loop's `just test` does not collect
`packages/core/tests`, this does not gate the loop — call it out
explicitly and do not rely on it for DoD. (§11 Q6.)

**No reproducing test precedes this** (it is an additive feature, not a
bug fix). The gating verifier tests ARE the executable specification of
coverage/faithfulness.

## 9. Risk assessment

- **Blast radius: low, and default-safe.** With
  `transition_verification_enabled=False` the verifier is never called
  and reflection is unchanged. Even enabled, it only reads two
  in-memory lists and logs/meters — it never mutates observations,
  never touches the CAS write, never blocks Phase 5. Reversibility:
  flip the flag or revert the seam edit.
- **Likeliest failure modes:**
  1. **Mis-scoping coverage as a hard error** — intentional evidence
     pruning (Phase 0 / refresh) legitimately drops evidence ids, so a
     low-coverage transition is a *flag for review*, not a defect.
     Record-only is mandatory; do not let a low score block Phase 5.
     This is the core "gate, don't block" risk. (§11 Q3.)
  2. **Over-flagging faithfulness on legitimate merges** — verify that
     Phase 4's normal output only ever cites evidence ids drawn from
     the `int_to_uuid` map built from source evidence
     (`reflection.py:1942`, `:2037-2038`); if so, faithfulness < 1.0
     genuinely means fabricated/out-of-bounds provenance and
     `faithfulness_min = 1.0` is safe. Confirm against the
     out-of-bounds branch (`reflection.py:2048-2051`) so the new signal
     and the existing warning agree rather than double-counting. (§11
     Q7.)
  3. **Wiring in the wrong place** — inserting before the
     `if not entity_summary` preservation guard (`:621`) vs after does
     not matter for evidence-set math (summary is not evidence), but
     the call MUST be after `final_obs` is bound (`:611`) and MUST NOT
     wrap or short-circuit the Phase-5 block (`:626-659`).
  4. **Test placed where the gate can't see it** — a verifier test
     under `packages/core/tests/` is invisible to `just test`. Root
     `./tests/` only for the gating tests.
  5. **Config default flips behaviour** — if the flag defaults to
     `True`, every reflection cycle in every environment starts
     emitting the new log/metric on day one. Decide deliberately (§11
     Q5).

## 10. Subtickets (ordered)

1. **Verifier module + verdict model** (`transition_verifier.py`) with
   coverage + faithfulness over evidence-id sets and empty-set guards.
   → verify: importable; `verify_reflection_transition` returns a
   `TransitionVerdict`; `just prek` clean.
2. **Gating unit tests** (`tests/test_transition_verifier.py`) — the six
   §8 cases. → verify: `just test` green; each case asserts the exact
   score + flag set.
3. **Metrics + config** — add `REFLECTION_TRANSITION_FLAGGED_TOTAL`
   (`metrics.py`) and the three `ReflectionConfig` fields (`config.py`).
   → verify: `just prek` clean; config parses with defaults; disabled
   default (if chosen) keeps reflection unchanged.
4. **Wire the Phase-4 seam** (`reflection.py:611`), record-only, flag
   the log + counters, gated on the config flag; Phase 5 untouched. →
   verify: reading the diff, Phase 5 control flow at `:626-659` is
   byte-identical; the call is gated and after `final_obs` is bound.
5. **(Optional) non-gating wiring test** in
   `packages/core/tests/unit/...`. → verify: passes under `uv run
   pytest packages/core/tests/...`; documented as non-gating.
6. **Adversarial review + close** — confirm record-only (no Phase-5
   block), real anchors, deterministic tests, default-safe config. →
   verify: `adversarial` pass green; §11 forks resolved or accepted.

## 11. Open questions (forks for the operator)

- **Q1 — Persist transition records now, or defer?** RFC proposes a
  `transition_log` table. *Recommendation:* **defer** — a new table +
  alembic migration + retention story is a ticket of its own, and
  structured-log + Prometheus recording satisfies "record, don't block"
  for the first slice. Revisit once flag volume is known. Flag, don't
  build.
- **Q2 — Preservation (corruption) via LLM entailment: this ticket or
  a follow-up?** Deterministic checks cannot detect meaning change of a
  retained claim, and `entity_summary` faithfulness needs entailment
  too. *Recommendation:* **follow-up ticket** reusing
  `ClassifyRelationships` (`contradiction/signatures.py:62`) via
  `run_dspy_operation`, config-gated and default-off (cost per
  transition). Leave a `preservation: float | None = None` hook in
  `TransitionVerdict`; do not compute it here.
- **Q3 — Hard-fail mode for clear-cut omission?** The triage asked to
  investigate whether blocking Phase 5 on a clear omission is safe.
  *Recommendation:* **no hard-fail in this slice — record only.**
  Phase 0/refresh legitimately prune evidence, so low coverage is not
  unambiguously an error; a block would drop legitimate consolidations
  and could livelock the reflection queue (Phase 5 abandon → re-enqueue
  → same block). The *faithfulness* dimension is the cleaner-cut case (a
  fabricated evidence id is never legitimate), so if any hard-fail is
  ever added it should be faithfulness-only and behind its own flag —
  but that is a separate, later decision, not this ticket. Operator to
  confirm record-only for now.
- **Q4 — Also verify the `_refresh_observation` transition
  (`reflection.py:765`)?** It re-synthesizes one observation onto a
  strictly-smaller evidence set — precisely an omission-prone path.
  *Recommendation:* **defer to a follow-up**; wire only the Phase-4
  seam here to keep the slice M. Note it explicitly so it is not
  forgotten.
- **Q5 — Default for `transition_verification_enabled`?**
  *Recommendation:* default **`True`** — the feature is record-only and
  default-safe (no behaviour change beyond a log/metric on genuinely
  suspicious transitions), and shipping it off would leave the gap
  #258 targets open until someone flips a flag. If the operator wants a
  quiet rollout, default `False` and enable per-environment. Operator's
  call.
- **Q6 — Does the wiring get a gate-collected test at all?** The loop
  gate runs only root `./tests/`; a wiring test needs core internals
  and lives under `packages/core/tests/`, so it cannot gate the loop.
  *Recommendation:* make the **pure verifier** the DoD-gating surface
  (fully tested at root), and add the wiring test as a documented
  non-gating supplement. Accept that the seam wiring is verified by
  review + the non-gating test, not by `just test`.
- **Q7 — Set `faithfulness_min = 1.0` (any unsupported id flags) or
  leave slack?** Phase 4 builds target evidence from an
  `int_to_uuid` map derived from source evidence
  (`reflection.py:1942`, `:2037-2038`), so in normal operation the
  target evidence set ⊆ source universe and `faithfulness == 1.0`.
  *Recommendation:* **1.0** — any drop below it is real fabricated /
  out-of-bounds provenance (the case already warned about at
  `reflection.py:2049`), making this a high-precision signal. Confirm
  the new counter and that warning agree (don't double-log the same
  event without noting it).

---

**Eval marker:** `.loop/config.json` sets `require_eval: true` — the
loop refuses pickup until this ticket's eval marker exists. It is
MANDATORY, authored with the `create-eval` skill before implementation
(the "eval is the spec" step). The eval should pin the verifier's
Definition of Done as a small scenario set: (a) a clean transition
scores coverage 1.0 / faithfulness 1.0 / no flags; (b) a dropped-source
-evidence transition flags `omission` with the expected coverage
fraction; (c) an unsupported-evidence-id transition flags
`hallucination`; (d) the guardrail — **Phase 5 still runs when a
transition is flagged** (record-only, no block); and (e) with the
config flag disabled, reflection output and side effects are unchanged.
