# Memex Cognitive Memory — Backlog

Source: [cognitive-memory-research-report.md](./cognitive-memory-research-report.md) (v6.9) · Wave 0 output: [WAVE-0-PREWORK.md](./WAVE-0-PREWORK.md) · **Agent rules: [BACKLOG-AGENT-RULES.md](./BACKLOG-AGENT-RULES.md) — read before picking up an item**
Last updated: 2026-05-01
Total scope: ~22-30 weeks for Tier S+A (incl. ~2-day Wave 0)

---

## Wave 0 — Executed (~2 days)

**See [WAVE-0-PREWORK.md](./WAVE-0-PREWORK.md) for the full verification + decision document.** Five specs refined or flipped against current source:

- [x] **W0.1** — All `file:line` references re-verified against current `main`; corrections folded into report v6.9
- [x] **W0.2** — Archive semantics: considered Option A (non-destructive on entity graph), then **reversed to Option B** (keep current destructive-cascade behavior). F4 (`memory_deprioritize`) is the designated non-destructive verb — two distinct verbs (archive = destructive cleanup; deprioritize = non-destructive downweight) beats one ambiguous verb. **No code change** to archive; F4's prompt-text must contrast the two. Bulk-archive (≥10 notes) keeps human-gating per §3.1.
- [x] **W0.3** — MW cold-start: **Beta-Bernoulli α=β=1, no warm-up gate** (works because of W0.5 formula)
- [x] **W0.4** — Test-impact quantification confirmed at 2026-04-29 baseline (108/117/17/74)
- [x] **W0.5** — F1b scope shrunk: parameterize via `apply_generic_filters` (one branch covers all 8 strategy sites); `document_search.py` excluded; scope matrix uses existing `include_stale` flag
- [x] **W0.6 (NEW)** — F1c MW formula corrected to **additive-marginal** `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)` (was pure-sigmoid which would zero cold-start units)
- [x] **W0.7 (NEW)** — F1a will remove dead `MemoryUnit.access_count` column (grep-verified: 0 write-sites beyond initializer, 0 read-sites in `packages/`, existing test asserts DTO absence)
- [x] **W0.8 (NEW)** — Bulk archive (≥10 notes) retains human-gating in §3.1 because the destructive cascade can vaporize a substantial fraction of a vault's synthesis in one call; single-note archive remains agent-autonomous (low blast radius; auditable; restoration via re-ingest + re-reflect)

---

## Now — Tier S Foundation

**4 features (F1 split into 3 sub-PRs), ~7-10 weeks. Build first.**

- [x] **F1a** — Schema + outcome API + DTO + API symmetry + `access_count` removal
  - Size: M (1.5-2w) · Effort: Moderate
  - Adds `success_co_count` / `failure_co_count` / `is_deprioritized` columns; new `memex_record_outcome` MCP tool; API symmetry: `reference_date` on `memex_note_search`, date params on `memex_survey`
  - **Removes dead `MemoryUnit.access_count` column + index** (Mn8 verified)
  - Default retrieval behavior unchanged (columns exist; no SQL site reads them yet)
  - F4 only depends on F1a (the `is_deprioritized` column)

- [x] **F1b** — Retrieval scope via `apply_generic_filters`
  - Size: S-M (1-1.5w) · Effort: Low (centralized, not per-site)
  - One branch in `apply_generic_filters` covers all 8 strategy sites; flag defaults false → no behavior change unless caller opts in
  - 4 surfaces total: function + RetrievalRequest + MCP tool + DTO
  - `document_search.py` and `engine.py:1357` explicitly NOT touched (per WAVE-0-PREWORK.md §3.3)
  - Resolves Appendix B C3
  - Stacks on F1a; can ship before F1c

- [x] **F1c** — MW soft-factor (additive-marginal) at reranker
  - Size: S (~1.5w) · Effort: Low (composition lands at `engine.py:1140`)
  - `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)` with `mw_alpha = 0.3` and Beta-Bernoulli α=β=1 prior; cold-start units get `mw_boost = 1.0` (neutral, no warm-up gate needed)
  - +0.5w over v6.8 estimate to budget for before/after benchmark
  - Stacks on F1a; F1b is independent

- [x] **F2** — D-MEM Z-score embedding anisotropy correction
  - Size: XS (~1w) · Effort: Low
  - Pure retrieval-precision fix; ships fastest
  - No dependencies; can ship anytime (parallel to Wave 0 or F1a)

- [x] **F25** — Write-time importance + intent + risk classifier
  - Size: M (4-6w) · Effort: Moderate
  - Subsumes F26; affects every future extraction
  - LLM cost per ingest — needs per-vault rate-limit

- [x] **F33** — MW exploration floor
  - Size: S (1-2w) · Effort: Low
  - Prevents rich-get-richer; depends only on F1a (data) — F1c makes injection meaningful
  - Can roll in alongside F1c in same wave

---

## Next — Tier A High-value

**11 features, ~14-21 weeks. Build after Tier S delivers signal.**

### Quick wins (~3-5w bundled)

- [x] **F5** — `memory_summarize_node` MCP exposure (~3d) — wraps existing reflection endpoint (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F4** — `memory_deprioritize` MCP tool (1-2w) — direct curation verb; depends on **F1a's** `is_deprioritized` column (not the full F1 stack) (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F38** — Consolidation orchestration (1-2w) — nightly batch over reflection + contradiction + cleanup (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F14** — Procedural observations via KV namespace (1-2w) — cross-agent value via Hermes briefings; collision policy: last-writer-wins + version + capped history (shipped 2026-05-01 via memory_augmentation, see final-report.md)

### Linter cluster (~5-7w bundled)

- [x] **F6** — Maintenance ledger + rule-based linter (4-6w) — runs under existing `MEMEX_LEADER_LOCK_ID` (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F8** — `memex_get_lint_flags` MCP (1-2w) — depends on F6 (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F10** — Surprise-gated LLM lint (3-5w) — depends on F2 + F6; LLM cost-capped per vault (shipped 2026-05-01 via memory_augmentation, see final-report.md; polarity discrimination deferred to F10b)

### Active learning (~7-10w)

- [x] **F20** — FSRS-based memory revisitation (4-6w) — depends on F1 + F25; Ebbinghaus heritage explicit (shipped 2026-05-01 via memory_augmentation, see final-report.md)
- [x] **F9** — `memory_reconsolidate` + per-entity advisory lock (4-6w) — new locking infra in `services/locks.py` (shipped 2026-05-01 via memory_augmentation, see final-report.md)

### Diagnostics

- [x] **F32** — Memory diagnostics (4-6w) — UMAP, heatmap, lint dashboard (shipped 2026-05-01 via memory_augmentation, see final-report.md)

### Subsumed

- ~~F26 — Risk classification at extraction~~ (rolled into F25)

---

## Later — Tier B (~triggers)

**10 features. Build only when the listed trigger appears.**

| # | Feature | Size | Trigger |
|---|---|---|---|
| F3 | Layer formalization (docs only) | XS (~3d) | Onboarding confusion appears |
| F7 | `memex lint review` interactive CLI | S (1-2w) | Lint backlog grows large |
| F10b | Polarity-discriminating NLI for surprise-gated lint (entailment / neutral / contradiction classifier on top of F10's `semantic_contradiction` signature) | S-M (2-3w) | ≥10 false-contradiction findings/month in production. Tier A scope-contracted in #22 (2026-05-01); v1 ships without polarity discrimination. |
| F11 | FSFM-lite decay scoring (Ebbinghaus × importance) | S-M (3-4w) | Reflection cron / lived experience shows stale-bias |
| F22 | Two-Factor edge confidence (variance) | M (3-4w) | Validation data becomes available |
| F24 | Reasoning-chain preservation | M (4-5w) | Re-examination becomes a use case |
| F27 | Comparative baseline registry | S (1-2w) | Multiple baselines exist (depends on regression habit) |
| F30 | Distilled extraction-time classifier | S (1-2w) | After F25 accumulates labeled data — cost optimization |
| F31 | Embedding-similarity intent fallback | XS (~1w) | Cheap fallback when F25 LLM unavailable |
| F35 | Non-stationary EMA mode for MW | S (1-2w) | MW drift observed |
| F36 | Outcome-confidence weighting in `record_outcome` | XS (~3d) | Cleaner signal — ships as F1 v2 |

---

## Tier-A Phase 3 carryover (added 2026-05-01)

Six follow-ups surfaced during Phase 3 adversarial review and close-out. All Tier-B; none are Tier-A blockers. Sourced from `.dev-team-artifacts/dev-tier-a-cognitive-memory/reviews/adversarial-phase-3.md` § "Phase 3 closeout".

- [ ] **F-followup-1** (MEDIUM, from original report M-2) — F29 `record_outcome` `success: bool` field accepts lax Pydantic v2 coercion. Symmetric pair to F20's bool-rejection at the quality field. Add a `BeforeValidator(_reject_bool_quality)`-style guard or change `success` to a string enum. Start from `packages/core/src/memex_core/services/outcomes.py` and the matching MCP/Hermes signatures.
- [ ] **F-followup-2** (MEDIUM, from original report M-3) — Aioclock leader-only periodic tasks are not serialized. Risk: two leader tasks could dispatch concurrently if the scheduler triggers them on overlapping ticks. Add a per-task lock or per-task serialization queue under `MEMEX_LEADER_LOCK_ID`. Start from `packages/core/src/memex_core/scheduler.py` and the `@clock.task` registrations.
- [ ] **F-followup-3** (MEDIUM, surfaced 2026-05-01 close-out) — `ConsolidationTick.vault_id` SQLModel definition lacks the `ForeignKey('vaults.id')` annotation. The SQL FK exists in migration `027_consolidation_ticks.py:100-105` with `ondelete=CASCADE`, so the database invariant holds; tighten the model declaration to match. Start from `packages/core/src/memex_core/memory/sql_models.py:1653`.
- [ ] **F-followup-4** (LOW, from original report L-1) — F10 `LintLLMQuota.count` constraint is a value-range nit. Clarify whether `count == 0` should be permitted at the `CheckConstraint` level and document the 24h rolling-window semantics inline. Start from `packages/core/src/memex_core/memory/sql_models.py` (`LintLLMQuota` definition) and migration `029_lint_llm_quota.py`.
- [ ] **F-followup-5** (LOW, from original report L-2) — F38 has no Hermes-exposed verb (intentional, scheduler-only per RFC-008), but no canary line documents this. Add a one-line comment in `packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py` and a pinning test so future audits don't re-flag the absence.
- [ ] **F-followup-6** (LOW, surfaced 2026-05-01 close-out) — `test_int_f8_lint_query.py::test_missing_table_raises_initialization_error` session-teardown is a latent cross-test contamination risk. The current `finally` block recreates the dropped table via `SQLModel.metadata.tables['maintenance_proposals'].create(checkfirst=True)`, but does not restore (a) FK constraints from rows in other tables, (b) indexes outside `SQLModel.metadata`, or (c) rows deleted via `ON DELETE CASCADE`. Tests pass in isolation; harden teardown. Start from `packages/core/tests/integration/services/test_int_f8_lint_query.py`.

---

## Skip — Tier C (evaluated, rejected)

| # | Feature | Why skipped |
|---|---|---|
| F12 | Pre-write D-MEM RPE gate | 53.9% real-turn skip rate — wrong for high-signal personal content |
| F13 | GDPR hard-delete | Build only when compliance requirement appears |
| F21 | Sleep consolidation (full ZenBrain port) | Replaced by F38 at ~1/8 the cost |
| F23 | Multi-judge retrieval eval harness | Enterprise-scale formalism; personal Memex doesn't justify 5-7w + ongoing LLM judge cost. User + daily reflection cron + spot-checks are sufficient signal. Reconsider only at multi-tenant or research-publication scale. Lightweight alternative: pinned 20-30 query regression set, manually verified (~1w per use). |
| F28 | TTL-based scheduled deprioritization | Subsumed by F25 + F11 |
| F29 | LoCoMo-Noise benchmark harness | F23 (now skipped) covered the core need |
| F34 | Forced retrieval diversification | MMR + F33 already cover this |
| — | TripleCopyMemory (ZenBrain B.8) | Overkill for personal scale |
| — | NeuromodulatorEngine 4-channel (B.6) | Out of scope; no use case |
| — | Bayesian Confidence Propagation (B.4) | Hard to validate without baseline |
| — | PriorityMap with emotion+goal (B.9) | Too rich for current evidence |
| — | MetacognitiveMonitor (B.11) | Bias detection interesting but not core |
| — | Cross-vault entity unification | `Entity` is already global |
| — | Adversarial memory-poisoning defense | Apr 2026 batch P0 — separate concern |
| — | Multi-judge ensemble for reflection | F23 (now skipped) covered the multi-judge case |

---

## Dropped during research (not in catalog)

- **F37** — Episode-recall TEMPR strategy → redundant. Memory_search has temporal strategy. Note_search handles drill-down. Topic-less time-window synthesis folded into F1's `memex_survey` extension.
- **F39** — Resolution-with-temporal-anchor → reverted in v6.4. F1's `record_outcome` + F4's `deprioritize` + free-text `reason` already handle the "issue resolved" case without new schema or enums.

---

## Implementation waves

| Wave | Duration | Features |
|---|---|---|
| **0 — Pre-conditions (executed v6.9)** | ~1.5 days | All 7 W0 items resolved; **no code change** (originally-planned archive `cascade_to_models=False` revert dropped — destructive cascade kept as intentional cleanup; F4 is the non-destructive verb). See [WAVE-0-PREWORK.md](./WAVE-0-PREWORK.md) |
| 1 — Foundation | 4-6w | F1a, F1b, F1c, F2, F33 |
| 2 — Write-time judgment | 3-4w | F25 |
| 3 — Agent control | 4-5w | F4, F5, F8 |
| 4 — Linter + orchestration | 5-7w | F6, F10, F38 |
| 5 — Procedural observations | ~1w | F14 |
| 6 — Active learning | 3-4w | F20 |
| 7 — Diagnostics | 3-4w | F32, F9 |

**Tight MVP:** F1a + F2 + F4 (~5-7 weeks) — F1a delivers the deprioritization column + outcome write API; F2 ships independently; F4 stacks on F1a. Adds the curation verb on top of MW data + the anisotropy fix without requiring F1b/F1c to be in production.

---

## Cross-cutting requirements

- All MW counters live on **vault-scoped tables** (`UnitEntity`, `MentalModel`, `MemoryUnit`); never on global `Entity` (multi-tenancy invariant)
- All maintenance tasks register under existing `MEMEX_LEADER_LOCK_ID`; no new advisory locks unless F9 (per-entity locks for reconsolidation)
- All new mechanisms emit Prometheus metrics + OpenTelemetry traces (§3.3)
- Hermes plugin (8 Stream-1 tools) updated alongside each MCP-touching feature (§7.5)
- Quality signal is reflection cron + user spot-checks (no formal eval harness — F23 skipped)

---

## Architectural principles (apply to every feature)

1. **Non-destructive curation by default** — adjust retrieval weights, don't delete
2. **Separate write-time judgment from retrieval-time scoring**
3. **Memory must be observable** (metrics + tracing + diagnostics)
4. **Retrieval-weight composition** — KinthAI principle, MW (F1), FSFM (F11), exploration (F33) all compose at the reranker — same thesis, multiple signals, single composition site (`engine.py:1116`)

See cognitive-memory-research-report.md §3 for full principles.
