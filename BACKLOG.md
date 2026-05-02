# Memex Cognitive Memory — Backlog

Source: [cognitive-memory-research-report.md](./cognitive-memory-research-report.md) (v6.9 + 2026-05-02 §3.4.1 latency-optimization addendum) · Wave 0 output: [WAVE-0-PREWORK.md](./WAVE-0-PREWORK.md) · **Agent rules: [BACKLOG-AGENT-RULES.md](./BACKLOG-AGENT-RULES.md) — read before picking up an item**
Last updated: 2026-05-02 (added Tier-A new tickets from §3.4.1 + §3.5 + §3.4.2 design discussions: F40 pre-reranker filter, F41 score cache, F42 model-size/quantization, F43 agent-surface codification (now also covers historical-query routing), F44 F33 bypass path, F45 F40 observability, F46 chunk→MU traversal, F47 contradiction-confidence post-reranker boost, F48 contradiction-confidence pre-reranker filter (single `apply_pre_filter` flag covers MW+FSFM+confidence branches), F49 contradiction-graph timeline tool; cross-refs from shipped F1a/F4 tightened so forward-looking work is on its own ticket, not described in finished items). **Rule**: PR-review findings on in-flight PRs are NOT backlog material — they must be fixed in the PR before merge.
Total scope: ~22-30 weeks for Tier S+A (incl. ~2-day Wave 0) + ~4-6w for Tier-A latency optimization (F40, F41, F42, F44, F45) + ~2-3w for F43 agent-surface codification + ~3-5d for F46 chunk→MU traversal + ~2-3w for F47/F48/F49 reranker composition extensions (contradiction-confidence boost + pre-filter + graph-walk timeline)

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

- [x] **F1a** — Schema + outcome API + DTO + API symmetry + `access_count` removal — see report §4 F1a + §3.4 (composition site). F1a ships the `success_co_count` / `failure_co_count` columns + `record_outcome` write API. **Forward-looking work tracked separately** (do NOT conflate with F1a): pre-reranker filter that READS these counters → F40; F33's bypass path for that filter → F44; observability for the latency reclaim → F45.
  - Size: M (1.5-2w) · Effort: Moderate
  - Adds `success_co_count` / `failure_co_count` / `is_deprioritized` columns; new `memex_record_outcome` MCP tool; API symmetry: `reference_date` on `memex_note_search`, date params on `memex_survey`
  - **Removes dead `MemoryUnit.access_count` column + index** (Mn8 verified)
  - Default retrieval behavior unchanged (columns exist; no SQL site reads them yet)
  - F4 only depends on F1a (the `is_deprioritized` column)

- [x] **F1b** — Retrieval scope via `apply_generic_filters` — see report §4 F1b
  - Size: S-M (1-1.5w) · Effort: Low (centralized, not per-site)
  - One branch in `apply_generic_filters` covers all 8 strategy sites; flag defaults false → no behavior change unless caller opts in
  - 4 surfaces total: function + RetrievalRequest + MCP tool + DTO
  - `document_search.py` and `engine.py:1357` explicitly NOT touched (per WAVE-0-PREWORK.md §3.3)
  - Resolves Appendix B C3
  - Stacks on F1a; can ship before F1c

- [x] **F1c** — MW soft-factor (additive-marginal) at reranker — see report §4 F1c + §3.4 (composition formula) + §3.4.1 (cold-start neutrality, additive-marginal rationale)
  - Size: S (~1.5w) · Effort: Low (composition lands at `engine.py:1140`)
  - `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)` with `mw_alpha = 0.3` and Beta-Bernoulli α=β=1 prior; cold-start units get `mw_boost = 1.0` (neutral, no warm-up gate needed)
  - +0.5w over v6.8 estimate to budget for before/after benchmark
  - Stacks on F1a; F1b is independent

- [x] **F2** — D-MEM Z-score embedding anisotropy correction — see report §4 F2
  - Size: XS (~1w) · Effort: Low
  - Pure retrieval-precision fix; ships fastest
  - No dependencies; can ship anytime (parallel to Wave 0 or F1a)

- [x] **F25** — Write-time importance + intent + risk classifier — see report §4 F25
  - Size: M (4-6w) · Effort: Moderate
  - Subsumes F26; affects every future extraction
  - LLM cost per ingest — needs per-vault rate-limit

- [x] **F33** — MW exploration floor — see report §4 F33 + §3.4.1 (rich-get-richer counter-pressure)
  - Size: S (1-2w) · Effort: Low
  - Prevents rich-get-richer; depends only on F1a (data) — F1c makes injection meaningful
  - Can roll in alongside F1c in same wave

---

## Next — Tier A High-value

**11 features, ~14-21 weeks. Build after Tier S delivers signal.**

### Quick wins (~3-5w bundled)

- [x] **F5** — `memory_summarize_node` MCP exposure (~3d) — wraps existing reflection endpoint — see report §4 F5 (shipped 2026-05-01 via memory_augmentation)
- [x] **F4** — `memory_deprioritize` MCP tool (1-2w) — direct curation verb; depends on **F1a's** `is_deprioritized` column (not the full F1 stack) — see report §4 F4 (shipped 2026-05-01 via memory_augmentation). **Forward-looking work tracked separately** (do NOT conflate with F4): agent-side codification of the §3.5 5-step flow + §3.4.1 gradient-vs-binary axes table → F43; chunk → memory_units traversal needed for §3.5 Option C → F46.
- [x] **F38** — Consolidation orchestration (1-2w) — nightly batch over reflection + contradiction + cleanup — see report §4 F38 (shipped 2026-05-01 via memory_augmentation)
- [x] **F14** — Procedural observations via KV namespace (1-2w) — cross-agent value via Hermes briefings; collision policy: last-writer-wins + version + capped history — see report §4 F14 + §2.3.1 (procedural memory rationale) (shipped 2026-05-01 via memory_augmentation)

### Linter cluster (~5-7w bundled)

- [x] **F6** — Maintenance ledger + rule-based linter (4-6w) — runs under existing `MEMEX_LEADER_LOCK_ID` — see report §4 F6 (shipped 2026-05-01 via memory_augmentation)
- [x] **F8** — `memex_get_lint_flags` MCP (1-2w) — depends on F6 — see report §4 F8 (shipped 2026-05-01 via memory_augmentation)
- [x] **F10** — Surprise-gated LLM lint (3-5w) — depends on F2 + F6; LLM cost-capped per vault — see report §4 F10 (shipped 2026-05-01 via memory_augmentation; polarity discrimination deferred to F10b)

### Active learning (~7-10w)

- [x] **F20** — FSRS-based memory revisitation (4-6w) — depends on F1 + F25; Ebbinghaus heritage explicit — see report §4 F20 (shipped 2026-05-01 via memory_augmentation)
- [x] **F9** — `memory_reconsolidate` + per-entity advisory lock (4-6w) — new locking infra in `services/locks.py` — see report §4 F9 (shipped 2026-05-01 via memory_augmentation)

### Diagnostics

- [x] **F32** — Memory diagnostics (4-6w) — UMAP, heatmap, lint dashboard — see report §4 F32 (shipped 2026-05-01 via memory_augmentation)

### Agent-surface codification (~2-3w, added 2026-05-02 from §3.5 rewrite)

**Source: report §3.5 "User-driven memory resolution: how should agents invoke F4?".** F4 (`memory_deprioritize`) and F1's `record_outcome` shipped as *tools* but the agent-side **flow** that uses them (disambiguate-first → cross-note coverage → mandatory LLM judgment → paired writes) is not yet documented anywhere agents will read it. Without this codification, agents will continue to dump every loosely-related MU into `record_outcome`/`deprioritize` on a vague resolution prompt, defeating the gradient-vs-binary axes design. **Per `feedback_use_memex_for_persistent_memory.md` and CLAUDE.md rule 24, this is not a single-surface fix — MCP, Hermes plugin, and Claude Code plugin must all carry the same guidance.**

- [ ] **F43** — Codify §3.5 5-step agent flow + axes-table guidance across MCP/Hermes/Claude Code surfaces (~2-3w) — see report §3.5 (worked example, 5-step flow, axes table, "What's genuinely adjacent work" enumeration) + §3.4.1 "MW is the gradient; deprioritize is the binary" subsection. *Estimate is 2-3w (bumped from 1-2w): 4-package coordination — MCP + Hermes + Claude Code + CLAUDE.md — plus real-LLM golden tests across all three surfaces.* **Scope (per CLAUDE.md rule 24, parity across all three agent surfaces — single PR touching all three; partial updates are non-mergeable):**
  1. **MCP server** (`packages/mcp/src/memex_mcp/server.py`): expand `memex_record_outcome` and `memex_memory_deprioritize` tool docstrings to teach the 5-step flow (Step 1: disambiguate when ambiguous → ask before writing; Step 2: route by info quality (`memex_find_note` for title-fragment, `memex_memory_search` only when title unknown) AND choose Option A/B/C for cross-note coverage; Step 3: mandatory LLM judgment over candidate set; Step 4+5: paired writes — `record_outcome(success=false)` AND `memory_deprioritize` against the *judged-relevant subset only*). Include the orthogonal-axes table verbatim.
  2. **Hermes plugin** (concrete files: `packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py`, `templates.py`, `tools.py`): add the 5-step flow + axes-table to the session-briefing primer (already carries the storage-model primer; this slots beside it). The verb-pair scaffolding belongs in `templates.py` so a Hermes turn can lean on a structured prompt rather than free-form generation.
  3. **Claude Code plugin** (concrete dirs: `packages/claude-code-plugin/rules/`, `packages/claude-code-plugin/skills/`, `packages/claude-code-plugin/hooks/`): update existing skills (e.g., `/remember`, `/recall`) AND add a new rule file teaching the disambiguate-first / Options-A/B/C / paired-writes pattern. Hooks may carry the imperfect-recall-by-design framing as a session-start reminder.
  4. **CLAUDE.md** (root): add a short "When the user reports an issue resolved" section pointing to the three above.
  - **Option A/B/C routing** (Step 2 of the flow):
    - **Option A** — entity-anchored: `memex_list_entities(query=…)` → `memex_get_entity_mentions(entity_id=…)`. Use when topic ↔ entity.
    - **Option B** — cross-note semantic: `memex_memory_search(query=…, after=…, top_k=30)`. **Note: top_k must be ≥30, not the default 5** — narrow top_k will miss cross-note matches.
    - **Option C** — single-note PageIndex traversal: `memex_get_page_indices(note_id)` → `memex_get_memory_units(chunk_ids=[…])` (or equivalent). Use when scope is provably one note.
  - **Imperfect-recall-by-design framing**: explicit statement that none of Options A/B/C give *provable* 100% recall, and that this is fine because **F33 exploration is the safety net** — units that slip past resolution will re-surface, the user re-confirms, and another `record_outcome(success=false)` compounds the MW penalty. User-driven resolution is a *gradient* across many turns, not a one-shot delete.
  - **Historical / audit-query routing rule (added 2026-05-02 from §3.4.2 investigation)**: the same five-step flow assumes the user is asking "what's true *now*". For queries about *how things changed* — "how has my view on X evolved", "what did I used to think about Y", "show me everything I've believed about Z including the wrong stuff" — the agent must route differently:
    - For ordered-chain timelines on a specific unit: call **F49** `memex_get_unit_history(unit_id)` — graph walk through contradiction links, returns predecessors in temporal order. Cleaner semantics than ranked search for "evolution" queries.
    - For broader audit / "show me everything including hidden stuff": call `memex_memory_search(query=…, apply_pre_filter=False)` — bypasses F40 + F48 (MW + FSFM + confidence pre-filters) so contradicted, behaviorally-failed, and decayed units appear. Post-reranker boosts (F47 confidence_boost, F1c MW) still apply, so contradicted units rank below clean ones — which is the right ordering for audit queries.
    - **Disambiguation triggers** the agent should learn: "evolved", "used to", "history of", "what changed", "what did I think before", "audit", "show me everything", "show me the hidden ones", explicit time-window-with-no-filter intent.
    - This is a *separate routing path* from the resolution flow (Steps 1–5). When the user says "the X issue is resolved", apply the resolution flow. When the user says "how has my position on X changed", apply the historical routing rule. Disambiguation is the agent's responsibility.
  - **What is *not* a gap** (resisted scope creep — keep out of F43, codify in agent guidance as "do NOT add"): combined `memex_resolve(unit_ids, reason)` endpoint; `resolved_at` timestamp column; `resolution_type` enum on deprioritize; `bulk-by-source` parameter on deprioritize; note-level deprioritize. The free-text `reason` field carries all needed information.
  - **Out of scope (separate adjacent work, not memory-core)**: Telegram cron-response handler — translates user chat replies into the right MCP calls. Lives in Hermes/Telegram integration code per §7.5. Not a memory-core ticket; called out here so it isn't silently expected of F43.
  - **Test plan (per `feedback_llm_output_validation.md`)**: drive a real-LLM agent turn ("Telegram notifications are now resolved") against each of the three surfaces (MCP direct, Hermes briefing, Claude Code session) and assert: (a) agent disambiguates first when scope is ambiguous; (b) calls `find_note` (or `note_search` only if title unknown) BEFORE any write; (c) for cross-note scope, calls one of Options A/B/C with `top_k>=30` if Option B; (d) issues paired writes (`record_outcome=false` AND `memory_deprioritize`) only against the LLM-judged-relevant subset, not every candidate. Use `pytest -m llm` markers; reuse golden-response patterns from existing real-LLM tests.
  - **Depends on**: F1a (`record_outcome`) + F4 (`memory_deprioritize`) + F46 (Option C path) + F40 (`apply_pre_filter` parameter) + F44 (F33 exploration bypass safety net) + F49 (`memex_get_unit_history` graph-walk tool, referenced by the historical-routing rule) — F1a/F4 shipped via memory_augmentation; F46/F40/F44/F49 sequence ahead of F43. No new code in core; pure agent-facing prompt work + tests.
  - **Acceptance**: real-LLM golden test passes on all three surfaces; single PR touching MCP + Hermes + Claude Code + CLAUDE.md (parity rule — partial PR is non-mergeable).
  - **Pre-merge sequencing gate**: F43's PR CI MUST verify that `apply_pre_filter` is a valid parameter on `RetrievalRequest` AND that `memex_get_unit_history` is a registered MCP tool — i.e., F40 and F49 are merged on the base branch. A trivial import-and-attribute-check test (e.g., `assert hasattr(RetrievalRequest, 'apply_pre_filter')` and `assert 'memex_get_unit_history' in get_mcp_tools()`) catches the sequencing failure before the F43 PR opens. This pre-merge gate is cheaper than discovering the dependency was unmerged via a real-LLM golden test that fails for the wrong reason.

### Reranker composition extensions (~1-2w, added 2026-05-02 from architectural-asymmetry investigation)

**Source: report §3.4.2 (added 2026-05-02) "Contradiction-derived confidence: completing the reranker composition".** Investigation prompted by user observation: F1c/recency/temporal compose explicitly as multipliers at `engine.py:1195`, but contradiction-derived `unit.confidence` is *not* in the composition. The contradiction engine adjusts `confidence` (α=0.1 weaken, 2α=0.2 contradict, per `packages/core/src/memex_core/memory/contradiction/engine.py:211-219`) and that confidence is loaded for display metadata (`unit.unit_metadata['superseded_by']`, engine.py:1051-1091) — but it has *no path into the reranker boost*. A contradicted unit and a non-contradicted unit, all else equal, get the same final reranker score today. The "downranking from contradiction" is folklore, not code.

- [ ] **F47** — Compose contradiction-derived confidence at the reranker site (1-2w) — see report §3.4.2 + §3.4 (KinthAI principle) + §3.4.1 (MW precedent — same architectural slot, different evidence type). **Concrete scope:**
  - Add a fourth multiplicative boost at `engine.py:1195` alongside MW / recency / temporal:
    ```python
    confidence_boost = 1.0 + confidence_alpha × (unit.confidence − 0.5)
    final = ce_score × recency_boost × temporal_boost × mw_boost × confidence_boost
    ```
  - **Cold-start / non-contradicted** (`confidence = 1.0`): boost > 1.0 (mild lift for clean units, mirrors MW's high-evidence success case).
  - **Single weakening** (`confidence = 0.9`): boost ≈ 1.0 (near-neutral).
  - **Single contradiction** (`confidence = 0.8`): boost < 1.0 (mild penalty).
  - **Repeatedly contradicted** (`confidence → 0.0`): boost → `1.0 − 0.5 × confidence_alpha` (substantial penalty).
  - `confidence_alpha` config field on `RetrievalConfig` (analogous to `reranking_mw_alpha`); tune empirically — once calibration data is available, target ~0.3 for parity with MW and validate via before/after benchmark.
  - **Default `confidence_alpha=0.0` (off) at ship time** — flip to a non-zero value only after calibration data accumulates (parallels F1c's `mw_alpha` start-after-counters-populate convention). Reason: with `confidence=1.0` as the schema default, any non-zero α at ship time gives every never-contradicted unit a multiplicative lift (e.g., α=0.3 → +15%) — most units get the boost, distorting the score distribution before the calibration that would justify it.
  - Add `CONFIDENCE_BOOST_OBSERVED` Prometheus histogram analogous to `MW_BOOST_OBSERVED` (engine.py:1162).
  - **Reranker stays content-only**: do NOT add `[CONTRADICTED]` text labels to `format_for_reranking` (the cross-encoder wasn't trained on Memex's marker; it would be hallucinating a meaning). All behavioral/structural signals compose at the same site.
  - **Tradeoffs to document and validate**:
    - α calibration risk: too aggressive → buries genuinely-contradictory observations the user wants to see surface. Too mild → no behavioral change. Pin to a benchmark query set, mirror F1c's calibration approach.
    - Behavioral change: existing retrieval results will shift (some currently-surfaced contradicted units drop). Run before/after on a representative workload before flipping the default-on.
    - Double-counting check: confirm `confidence` isn't already used to FILTER candidates upstream (e.g., at hydration). Currently engine.py:1052 only loads supersession metadata for `confidence < 1.0` — that's a display path, not a filter, so no double-count. Verify no other site silently filters on confidence before promoting F47 to default-on.
    - Per the Memory Worth paper precedent (2604.12007v1 Theorem 4.1 caveat in §2.1): do NOT claim convergence guarantees for `confidence_boost` either — it's a useful Bayesian-flavored ranking signal in practice, not provably-convergent under non-stationary multi-tenant ingestion.
  - **Composition variance bounds**: post-F47 the reranker composition becomes `final = ce_score × recency_boost × temporal_boost × mw_boost × confidence_boost` — four multiplicative boost factors on top of `ce_score`. With α=0.3 each, the boost-only dynamic range is roughly `0.85⁴ ≈ 0.52` → `1.15⁴ ≈ 1.75`, a ~3.4× compounded range. Once F11's `importance_decay_boost` joins the chain (same composition site), this becomes five factors and the range widens further. Practical implication: MMR diversity λ may need re-tuning as additional boost factors land — call this out as an F11/F47 follow-up checkpoint to verify the diversity penalty still gates appropriately against the widened score distribution. **No spec-level clamp is added now (YAGNI)** — the boosts are intentionally additive-marginal and α-tunable, so observed behavior on a representative workload is the right gate. If before/after benchmarks show score-distribution compression (top-K candidates pile near the same final score so MMR can't differentiate), a `max_boost_compound` clamp on the product of boost factors can be added in a follow-up ticket.
  - **Depends on**: nothing new — `unit.confidence` is already being adjusted by the contradiction engine; the column already exists; the composition site is the same one F1c uses.
  - **Acceptance**: before/after benchmark shows quality lift on contradicted-unit queries when `confidence_alpha` is dialled up off the `0.0` default; observability metric (`CONFIDENCE_BOOST_OBSERVED` histogram) shows boost distribution matches expectations from the contradiction-frequency in the test vault; all existing tests still pass with the shipping default `confidence_alpha=0.0` (composition is a no-op).

- [ ] **F48** — Confidence pre-reranker filter (third OR'd branch in F40's predicate) (~3-5d) — see report §3.4.2 ("Pre-reranker confidence filter: parallel to F40's MW filter"). **Pattern**: same architecture as F40 (MW pre-filter) and F47 (confidence post-reranker boost) — strongly-contradicted units that get heavily multiplied down by F47's boost are paying full reranker cost only to be downweighted past usefulness. Filter them before the cross-encoder.
  - **SQL extension to F40's predicate** (third OR'd clause):
    ```sql
    WHERE NOT (
        -- MW branch (F40)
        ((success_co_count + failure_co_count) >= 5 AND (success_co_count + 1.0) / (success_co_count + failure_co_count + 2.0) < 0.15)
        OR
        -- FSFM branch (F40, no-op until F11 ships)
        (importance × exp(-elapsed/stability) < 0.10)
        OR
        -- Confidence branch (F48 — strongly-contradicted units)
        (confidence < 0.2)
    )
    ```
  - **Cold-start protection**: a never-contradicted unit has `confidence = 1.0` (the schema default) — never excluded by this branch. Only units with multiple repeated contradictions (each `-2α = -0.2`) drop below the threshold. The evidence-threshold sub-condition needs design — options:
    - (a) Count of incoming `contradicts` / `weakens` `MemoryLink` rows ≥ 2 (i.e., needs at least two corroborating contradictions before pruning, parallel to F40's `>= 5 outcomes` threshold).
    - (b) No evidence threshold — just `confidence < 0.2`. Simpler; relies on the contradiction engine's α-step already requiring multiple events to reach that floor (default α=0.1 means ≥5 contradict events or ≥9 weaken events).
    - **Recommendation: (b) initially**. The α-stepping IS the evidence accumulation — adding a count threshold is double-counting. Re-evaluate if false-prunes appear.
  - **F33 exploration runs on the same separate retrieval path that bypasses F40's filter** (per F44) — so F48's pruned units also get re-validation cycles. F44 doesn't need extension; the bypass already covers any predicate added to the main hydration query.
  - **Compute model: on-the-fly at hydration** (same as F40) — `confidence` is already a column on `MemoryUnit`, no new schema, no new index needed beyond what F40 adds.
  - **Expected savings**: marginal additional latency win on top of F40 — depends on contradicted-unit frequency in the vault. In a vault with active contradiction detection, expect another ~5–10% of candidates dropped pre-rerank.
  - **Sequencing**: ship F48 *with* F40 (same hydration-query change) OR immediately after. Since F47 (post-reranker boost) and F48 (pre-reranker filter) compose, ship F47 first to validate the signal direction, then F48 once observability metrics confirm low false-prune rate.
  - **Per-query bypass**: F48's branch sits inside F40's `WHERE NOT (...)` predicate, so it inherits F40's `apply_pre_filter: bool = True` flag — no separate parameter. When the user/agent is auditing contradiction history, `apply_pre_filter=False` returns contradicted units alongside everything else. The post-reranker `confidence_boost` (F47) still applies in that mode — i.e., contradicted units appear but rank below clean ones, which is the right behavior for audit queries.
  - **Depends on**: F40 (filter infrastructure + bypass flag) + F47 (validates that confidence is a useful ranking signal before relying on it as a filter); F44 (F33 bypass, already covers this filter via the same path).
  - **Acceptance**: F48-pruned-candidate-count metric ≤ 10% of typical hydration set; F47's `CONFIDENCE_BOOST_OBSERVED` histogram shows that pruned units would have been multiplied down by ≥50% had they reached the reranker; before/after retrieval-quality benchmark shows no regression on contradicted-but-recoverable units (those should be re-surfaced via F33 bypass).

- [ ] **F49** — Contradiction-graph timeline traversal tool (~3-5d) — see report §3.4.2 ("Dedicated path: contradiction-graph traversal for timeline queries"). The `apply_pre_filter=False` bypass on `memex_memory_search` (F40 + F48) lets historical/audit queries see contradicted units, but it returns a *ranked semantic set*, not an *ordered chain*. For "how has my view on X evolved" queries, walking the contradiction graph directly gives cleaner semantics. **Concrete scope:**
  - New MCP tool `memex_get_unit_history(unit_id, max_depth=10)` — starts from a unit, walks backward via `MemoryLink` rows where `link_type IN ('contradicts', 'weakens')` and `from_unit_id = current_unit_id`, collecting `to_unit_id` values as predecessors. Each hop returns `(predecessor_unit, link_type, link_metadata.reasoning, timestamp_inferred_from_authoritative_unit_id_or_link_created_at)`. *`reinforces` links are excluded from the default backward traversal because they point forward in time (newer unit reinforcing an older one) — walking them backward inverts the timeline. A future extension can add a `forward=True` mode that walks `reinforces` separately, but the v1 timeline is strict supersession history.* The v1 timeline shows **supersession history** (negative-evidence path: contradicts/weakens), not full **confidence evolution** (which would also include positive-evidence `reinforces` events). A future `forward=True` extension can walk `reinforces` separately for the full evolution view.
  - **Link direction convention**: per `packages/core/src/memex_core/memory/contradiction/engine.py:225-227`, `MemoryLink` is constructed as `from_unit_id=authoritative.id, to_unit_id=superseded.id` — i.e., the link points from the *winner* (typically the newer/contradicting unit) to the *loser* (typically the older/contradicted unit). To walk backward in time from a current unit toward its predecessors, query `WHERE from_unit_id = current_unit_id` and collect `to_unit_id` values. (For reference: a forward walk later — `forward=True` — would query `WHERE to_unit_id = current_unit_id` collecting `from_unit_id`, but that's not in v1.)
  - Returns an ordered chain (oldest → newest), NOT a ranked set. No reranker, no boosts, no quality filtering. Pre-filters do NOT apply — graph walk is for completeness, not relevance.
  - Vault-scoped via the standard mechanism. Per CLAUDE.md rule 24, expose on all three agent surfaces (MCP server, Hermes plugin, Claude Code plugin / docs).
  - **Edge cases to handle**: **Cycles and revisited nodes**: walk uses both a `visited: set[UUID]` guard AND a `max_depth` cap. The `visited` set prevents re-processing the same node via different predecessor paths in branching DAGs (example: if A is weakened by both B and C, and B/C are each weakened by D, the naive walk visits D twice; the visited-set ensures D is processed once). The `max_depth` cap is the second line of defense against actual cycles. Tests MUST assert both: (a) DAG-with-shared-predecessor returns each node exactly once; (b) literal cycle (synthetic test fixture) terminates at `max_depth` without recursion overflow. Branching predecessors (a unit weakened by two distinct contradictions — return both as parallel chains); orphan starting units (no contradiction links — return `[unit_only]`).
  - **Composition with F47**: the timeline doesn't apply `confidence_boost`. Confidence is *itself* the artifact the timeline is exploring — multiplying by it would be circular.
  - **Depends on**: nothing new — `MemoryLink` rows of the relevant types are already created by the contradiction engine (`packages/core/src/memex_core/memory/contradiction/engine.py:225-235`). Pure read-side tool.
  - **Acceptance**: real-LLM golden test with a vault containing 3+ contradiction events on a single conceptual chain; tool returns the chain in correct order; agents in F43 codification know to route "how has my view on X evolved" / "what did I used to think about X" to this tool, not to `memex_memory_search`.

### Latency optimization (~3-5w bundled, promoted from Tier B by 2026-05-02 measurement)

**Source: report §3.4.1 "Compute cost considerations: pre-reranker filtering as a Tier A optimization".** Cross-encoder reranker measured at **~1.5 s @ 70-candidate cap** in production (May 2026). At observed worst case ~30/70 candidates are deeply low-MW or low-FSFM — ~640 ms of reranker compute spent on units that get multiplicatively downweighted past usefulness. Promoted from Tier B because the latency cost (>40% reranker budget wasted) outweighs the architectural-simplicity argument.

- [ ] **F40** — Guarded pre-reranker MW/FSFM filter (1-2w) — see report §3.4.1 ("The guarded pre-filter design" + "Compute model: on-the-fly at hydration"). **Concrete scope:**
  - Add `WHERE NOT (...)` clause to the **hydration query** that fetches full unit bodies for the RRF top-K (alongside the existing `unit_id IN (...)` and `is_deprioritized = false` filters).
  - **MW branch (ships in F40):** `(success_co_count + failure_co_count) >= 5 AND (success_co_count + 1.0) / (success_co_count + failure_co_count + 2.0) < 0.15` — `mw_score` is derived inline (Beta-Bernoulli α=β=1 closed form); there is no `mw_score` column.
  - **FSFM branch (no-op until F11 ships):** `COALESCE(importance * exp(-EXTRACT(EPOCH FROM (now() - last_outcome_at)) / 86400.0 / NULLIF(stability, 0)) < 0.10, FALSE)`. F11 adds `stability`, `last_outcome_at` on `memory_units`, and the importance signal from F25; F40 ships the predicate inactive-by-default and F11 activates it with no breaking change. **Edge case — zero `stability`**: `NULLIF(stability, 0)` returns NULL when `stability = 0`, propagating to a NULL branch result; the `COALESCE(..., FALSE)` wrap then treats this as "do not filter," so zero-stability units are NOT pruned by the FSFM branch. Semantically debatable (`stability = 0` ≈ instant decay → arguably should filter), but acceptable because (a) F11 should not produce zero-stability values via legitimate decay paths, and (b) zero-stability is a degenerate state that observability (F45) will surface for diagnosis. If F11 finds this happens in practice, add an explicit `OR stability = 0` clause in a follow-up.
    - **Magic-number documentation**: `86400.0` is `STABILITY_SECONDS_PER_DAY` — the unit conversion factor between `EXTRACT(EPOCH ...)` (seconds) and `stability` (days). If F11 ever changes `stability`'s unit convention, this divisor MUST change in lockstep. Implementations should pull the literal from a named constant in the FSFM-builder module (`STABILITY_SECONDS_PER_DAY = 86400`) — NOT inline the bare number — so a unit change is one-edit.
  - **NULL-handling cold-start commitment**: when `stability` or `importance` is NULL on unclassified units (pre-F25/F11), the inner FSFM expression evaluates to NULL. Under SQL three-valued logic, `FALSE OR NULL OR FALSE → NULL`, then `NOT NULL → NULL`, then `WHERE NULL` excludes the row — the *opposite* of cold-start safety. The FSFM branch MUST therefore evaluate to FALSE (not NULL) on NULL-column rows so the filter keeps them. Two equivalent guards land the branch correctly: (1) wrap the branch in `COALESCE(..., FALSE)` (the form used in the SQL above — shorter); or (2) prefix with `importance IS NOT NULL AND stability IS NOT NULL AND last_outcome_at IS NOT NULL AND <expr> < 0.10`. Implementations MUST NOT COALESCE individual columns (e.g., `COALESCE(stability, 1.0)`) — that masks real values with synthetic defaults; the two guards above operate on the *branch-result* level, not the column level. Tests MUST assert that NULL-column rows pass through unfiltered when only the FSFM branch is active (i.e., MW + Confidence branches FALSE). The same TVL concern would apply to F48's confidence branch if `confidence` were ever NULL — but `confidence` has a NOT NULL DEFAULT 1.0 in the schema, so TVL never arises there and the F48 branch needs no COALESCE wrapper.
  - **FSFM branch ships inert at F40 ship time.** The SQL clause is gated by a feature flag (`fsfm_branch_enabled: bool = False` on `RetrievalConfig`), default OFF. F11 ships the column migration (`stability`, `importance`, and `last_outcome_at` on `memory_units`) AND flips the default ON in the same PR. Until F11, the WHERE-clause builder skips the FSFM branch entirely — no SQL referencing missing columns is emitted, so F40 cannot break a vault that hasn't yet had F11's migration applied. **`last_outcome_at` migration provenance**: the column does NOT exist on `memory_units` today — F1a's migration scope was `success_co_count`, `failure_co_count`, `is_deprioritized`, and the `record_outcome` write API only. Migration `028_procedure_outcomes.py` adds a `last_outcome_at` column on the *separate* `procedure_outcomes` table (KV procedural memory, not `memory_units`), so it does not satisfy F40's reference. F11 owns the `memory_units.last_outcome_at` migration alongside `stability` and the `importance` signal from F25.
  - **Implementation pitfall — Python-level conditional, NOT a runtime SQL flag**: the WHERE-clause builder must conditionally *include or omit* the FSFM SQL fragment in Python before query construction, NOT use a SQL-side `(NOT :fsfm_enabled OR ...)` guard. SQL-side guards still reference the missing column names at parse time and crash on `column "importance" does not exist`. Tests MUST assert that the generated SQL string does NOT contain `importance`, `stability`, or `last_outcome_at` substrings when `fsfm_branch_enabled=False`. This pinning test is what prevents an implementer from "fixing" the COALESCE-wrap with a runtime flag.
  - **OR'd, not AND'd** — either signal is sufficient grounds to skip the cross-encoder (a behaviorally-failed-but-recent unit is pruned by MW; a temporally-stale-but-never-retrieved unit is pruned by FSFM; AND'ing would underprune).
  - **Cold-start safeguards** (load-bearing — must not regress): MW branch's `>= 5 outcomes` clause keeps zero-outcome units in the candidate set; FSFM branch's `exp(elapsed)` term keeps fresh units neutral; F33's exploration path bypasses the filter entirely (see F44).
  - **On-the-fly compute, NOT precomputed columns**: filter scope is ~200 RRF candidates → `exp()` per row <1 ms total. Precomputing would add migration + trigger + drift-reconciliation surface for a sub-millisecond gain. Re-evaluate only if profiling shows hydration-time evaluation > 5 ms p95 OR if FSFM surfaces in user-facing query paths beyond pre-filter.
  - **Expected savings**: ~30% reranker latency reduction (~1.5 s → ~1.05 s) with no quality loss on the obviously-failed tail.
  - **Per-query bypass**: add `apply_pre_filter: bool = True` to `RetrievalRequest`, plumbed via `apply_generic_filters` (F1b's pattern). Default ON for everyday recall. When `False`, the entire `WHERE NOT (...)` clause drops out — every branch (MW + FSFM + the F48-added confidence branch) is bypassed in one go. Single flag because the three filter signals share one cognitive model ("things the system normally hides"); user/agent isn't reasoning about MW vs FSFM independently. F43 codification teaches the rule: historical / audit / lineage queries → `apply_pre_filter=False`.
  - **Depends on**: F1a (counters); F33 + F44 (exploration bypass path); FSFM clause is no-op until F11 (Tier B) ships.
- [ ] **F44** — F33 exploration retrieval-path: pre-filter bypass (~3-5d) — see report §3.4.1 safeguard 3 ("F33 exploration runs on a separate retrieval path that bypasses this filter"). F33 was shipped without this concept because the pre-filter didn't yet exist; F40 introduces the filter and depends on F33's path bypassing it for the self-correction property to hold. **Concrete scope**: F33's candidate fetch must (a) issue a separate hydration query that omits the F40 `WHERE NOT (...)` clause, (b) mark results as exploration-injected (existing F33 infrastructure), (c) reuse MMR for diversity at the merge with the main path's candidates. Without F44, F40 silently breaks F33 — a low-MW unit can never re-surface, and MW becomes monotonic. **Depends on**: F40 (filter) + F33 (already shipped). Can ship in same PR as F40, or separately if smaller PR is preferred — F40 should be shipped behind a feature flag if F44 lags.
- [ ] **F45** — F40 observability + re-evaluation gate metrics (~3d) — see report §3.4.1 ("When precomputation would become justified"). Emit metrics that drive the re-evaluation decision and validate the latency-reclaim claim: (a) hydration-query p95 (re-evaluation gate at >5 ms); (b) candidates-pruned histogram per query (validates the ~30% reclaim assumption empirically); (c) cross-encoder candidate count post-filter (should drop from 70 to ~50 in the typical case); (d) F33 vs main-path candidate count (validates F44 bypass is actually firing). Per cross-cutting requirement #3 (Prometheus metrics + OpenTelemetry traces). **Depends on**: F40 + F44.
- [ ] **F46** — Chunk → memory_units traversal MCP tool (~3-5d) — see report §3.5 Option C ("PageIndex traversal" — `memex_get_memory_units(chunk_ids=[…])`). The §3.5 worked example calls this primitive but it doesn't exist today: `memex_get_memory_units` only accepts `unit_ids`, and there's no MCP/Hermes/Claude-Code tool that returns all memory units belonging to one or more chunks. The schema supports it cheaply — `MemoryUnit.chunk_id` FK + `idx_memory_units_chunk_id` btree index already exist (`packages/core/src/memex_core/memory/sql_models.py:690,710`). **Concrete scope**: either (a) add a `chunk_ids: list[UUID] | None` parameter to `memex_get_memory_units` (preferred — single tool, mirrors `unit_ids` ergonomics), OR (b) add a new tool `memex_list_chunk_memory_units(chunk_ids)`. SQL is `SELECT * FROM memory_units WHERE chunk_id = ANY(:chunk_ids)` with vault-scoping filter. Per CLAUDE.md rule 24 (agent-surface parity), expose on all three surfaces (MCP server, Hermes plugin tools, Claude Code plugin if it has its own surface). **Depends on**: nothing (independent). **Unblocks**: F43's Option C path actually being callable end-to-end — without F46, F43's PageIndex-traversal guidance is aspirational.
- [ ] **F41** — Cross-encoder score cache (1-2w) — see report §3.4.1 (latency levers list). Keyed on `(embedding_model_version, query_embedding_hash, unit_id)` with 24h TTL. Repeat queries (e.g., daily morning briefing loop, recurring user templates) get free reranking. Biggest single lever for narrow query patterns; effectiveness depends on workload. Independent of F40 — they compose. Add cache-hit-rate metric to validate impact.
  - **Structural invalidation on model change**: including `embedding_model_version` (or an equivalent namespace prefix) in the cache key makes invalidation **structural rather than procedural** — a model upgrade silently lookups by the new version-prefix without serving stale entries from the old one. The 24h TTL still backstops other invalidation paths. No deploy-runbook flush step is required for embedding-model upgrades; the version is in the key, automatic invalidation handles upgrades.
  - **Concurrency model — in-process with `asyncio.Lock`-guarded check-and-fill (v1)**: a TTLCache (e.g., `cachetools.TTLCache`) keyed on `(embedding_model_version, query_embedding_hash, unit_id)` with per-key `asyncio.Lock` to prevent stampede on cache miss. Two concurrent requests for the same key MUST NOT both issue cross-encoder calls — the first acquires the key's lock, computes, fills the cache; the second waits on the lock, observes the filled entry, returns. **Redis-backed shared cache is a v2 option** (single-process cache is sufficient for current single-server deploy; Redis enters scope only when horizontal scaling lands, tracked separately). The default-on flag is `cross_encoder_cache_enabled: bool = True` on `RerankConfig`.
  - **Hash function — `xxhash.xxh64`**: chosen for speed (~10× faster than SHA-256) on the embedding-vector input. Collision probability on a 1M-row vault is negligible (~6×10⁻⁸ for a 1M item working set in a 64-bit space). A collision returns a wrong cross-encoder score for one cache entry — not a security concern, but bounded by the 24h TTL. SHA-256 is overkill for cache-key purposes; xxh64 is the right tool.
- [ ] **F42** — Cross-encoder model-size / quantization tuning (1-2w) — see report §3.4.1 (latency levers list). Int8-batched inference (typically 2× speedup, low recall risk) and/or smaller reranker model behind an A/B with recall-impact metric. Sequence after F40+F41 since this is the only lever with quality regression risk.

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
| 8 — Tooling gap (PageIndex traversal) | 3-5d | F46 (independent unblocker; ships first because F43's Option C guidance depends on it being callable end-to-end) |
| 9 — Latency optimization bundle | 4-6w | F40, F44, F45 bundled (F40 + F44 must ship together or behind feature flag; F45 lands with them). F40 introduces the `apply_pre_filter` parameter that F43 (Wave 11) routes against. F11 (Tier B) needed only for the FSFM clause inside F40 to activate. |
| 10 — Graph-walk timeline tool | 3-5d | F49 graph-walk timeline tool — moved ahead of F43 because F43's historical-routing rule references it (`memex_get_unit_history(unit_id)` is the dedicated path for "how has my view on X evolved" queries). F49 is independent of F47/F48 and ships standalone. |
| 11 — Agent-surface codification | 2-3w | F43 (depends on Waves 8–10 — needs F46 for Option C path, F40 for the `apply_pre_filter` parameter, F44 for the F33 exploration safety net, F49 for the historical-routing rule's graph-walk path; F1a/F4 already shipped). All four agent-facing surfaces (F46/F40/F44/F49) are pre-conditions — F43 codifies them across MCP + Hermes + Claude Code + CLAUDE.md. |
| 12 — Reranker composition extensions | 2-3w | F47 (post-reranker confidence boost) → F48 (pre-reranker confidence filter, riding F40's `apply_pre_filter` flag). Sequence F47 first to validate the signal direction, then F48 once observability confirms low false-prune rate. Both feed into F43's historical-routing-rule extension via the `apply_pre_filter=False` audit path. |
| 13 — Latency follow-on | 2-4w | F41 (cross-encoder score cache), F42 (model-size / quantization tuning). Sequence after F40+F41 since F42 is the only lever with quality-regression risk; both compose with Wave 9. |

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
