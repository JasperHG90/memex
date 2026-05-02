# Research Report: Memex Cognitive & Governance Architecture

**Date:** 2026-04-29
**Scope:** Comprehensive synthesis of four agentic-memory papers (ZenBrain, FSFM, D-MEM, Memory Worth) and three open issues (#18, #53, #64), with feature catalog covering existing plan items and full-paper-scan additions. Each Tier S/A feature is specified across six dimensions: source citation, new code location, code adapted, impact (effort/time/architecture/cascades), surface impact (CLI / MCP / Hermes plugin), and agent-facing prompt text.
**Recipient:** @JasperHG90
**Revision:** v6.9 — Wave 0 executed (see [`WAVE-0-PREWORK.md`](./WAVE-0-PREWORK.md)). Five v6.8 specs refined after source verification: archive Option A (non-destructive on entity graph) considered then **reversed** — kept current destructive-cascade behavior because F4 (`memory_deprioritize`) is the designated non-destructive verb; two clean verbs beat one ambiguous verb. MW cold-start simplified to α=β=1 (no warm-up gate); MW formula corrected to additive-marginal (no cold-start zeroing); F1b scope reduced to 4 surfaces via `apply_generic_filters` centralization; F1a now removes dead `access_count` column. See Revision history at end.

---

## 1. Executive summary

Memex's strategic gap is **governance + observability**, not capacity. The current architecture has rich extraction, layered storage, and multi-strategy retrieval, but no observable signal for which information earns its keep, no audit layer for graph degradation, no mechanism for an agent to actively curate, and no benchmark that lets us measure whether changes actually help.

The four papers and three open issues converge on three architectural principles:

1. **Non-destructive curation by default** — adjust retrieval weights, don't delete (KinthAI; MW §6).
2. **Separate write-time judgment from retrieval-time scoring** — classify intent and risk on ingest; score outcome on retrieval (FSFM §3.2; D-MEM §4.1).
3. **Make memory observable** — diagnostics, evaluation harness, lint findings, MW scores must be queryable and visualizable (D-MEM Appx A; ZenBrain Appx N).

The catalog below proposes ~30 features across four tiers. Tier S (after v6.7: F1, F2, F25, F33; F1 split into F1a/F1b/F1c sub-PRs in v6.8) is foundation infrastructure that unlocks the rest. Tier A (11 features) adds substantive capabilities. Tier B (10 features) is worth considering when reflection-cron observation or lived experience surfaces a need. Tier C (~14 features incl. F23) is rejected with reasons.

The full Tier S + A implementation is roughly **5-8 months of focused work** broken into eight waves (Wave 0 added v6.8 for pre-conditions; executed in v6.9 — see [`WAVE-0-PREWORK.md`](./WAVE-0-PREWORK.md); total 22-30 weeks). Tier S alone is ~7-10 weeks (Waves 0+1+2) and is sufficient to deliver observable governance. The tightest MVP — F1a + F2 + F4 — is ~5-7 weeks and delivers the deprioritization curation verb on top of MW data + the anisotropy fix.

---

## 2. Strategic context

### 2.1 Paper validity, recapped honestly

| Paper | arXiv | Strongest evidence | Strongest unsupported claim |
|---|---|---|---|
| ZenBrain | 2604.23878v1 | Sleep loop +37% stability, +47% faster learning (§H.5); judge-normalized answer quality wins at Bonferroni significance (§F.2) | Letta beats it on retrieval-proper P@5/MRR/NDCG (§F.1); H1/H2 hypotheses unsupported (§F.4) |
| FSFM | 2604.20300v2 | 30% memory reduction, 1.31× retrieval speedup, 100% security elimination on China Mobile data (§6.1); §4.5.1 head-to-head shows importance-weighted decay beats pure Ebbinghaus and no-decay baselines | Domain mismatch — telecom service data ≠ personal research notes; F23 must validate on Memex's content lifecycle |
| D-MEM | 2603.14597v1 | 80% token reduction at 75% noise injection (§5.2); embedding anisotropy is real (§4.1) | Critic Router skips real turns more than noise (53.9% vs 43.2%, §6.1) — accuracy/efficiency trade-off |
| Memory Worth | 2604.12007v1 | Two-counter formulation captures evidence amount alongside ratio (§3.4) | Almost-sure convergence (Theorem 4.1) **does not apply to Memex** — multi-tenant continuous ingestion is non-stationary |

**Production reference:** @kinthaiofficial comment on #64 (anonymous; treat as design intuition, not evidence).

### 2.2 What I underweighted in earlier revisions

The v2/v3 plans focused on **retrieval-time governance**. The full paper scan reveals three additional dimensions:

- **Write-time judgment** (FSFM ImportanceScoringEngine §3.2; D-MEM intent classification §4.1; KinthAI Permanent/Durable/Ephemeral)
- **Active learning / revisitation** (Ebbinghaus 1885 forgetting curve as foundation; ZenBrain FSRS §B.2; FSFM spaced repetition §5.5; ZenBrain sleep consolidation §B.3)
- **Diagnostics and evaluation** (D-MEM UMAP/heatmap §A.2-A.3; ZenBrain LLM-as-Judge methodology §Appx N; FSFM PerformanceBenchmarkingTool §3.2)

Tier S now includes F25 (write-time intent), in addition to the original governance core (F1, F2, F33). (F23 eval harness was originally added here but demoted to Tier C in v6.7 — personal-scale doesn't justify it.)

### 2.3 Memory types and how Memex covers them

Three useful framings, increasingly opinionated:

**Classic cognitive science taxonomy:**
- **Sensory** — milliseconds, raw perceptual buffer
- **Working / Short-term** — seconds–minutes, active manipulation
- **Long-term**
  - *Declarative (explicit)*
    - **Episodic** — events with time + place ("I deployed v2 on Tuesday and it broke")
    - **Semantic** — decontextualized facts ("v2 uses the new auth middleware")
  - *Non-declarative (implicit)*
    - **Procedural** — skills, how-to ("the deploy sequence is tag → push → monitor")
    - Priming, classical conditioning (not applicable to agents)

**ZenBrain's 7-layer expansion** (arXiv:2604.23878v1 §3.1): Working, Short-Term, Episodic, Semantic, **Procedural**, **Core** (identity/values that rarely change), **Cross-Context** (transfers between sessions/projects).

**KinthAI's pragmatic 3-way split** (#64 comment): *Do something* → procedural; *Know something* → semantic; *Need context* → episodic. This is the framing the agent surface should reflect.

**4-tier consolidation hierarchy** (rohitg00 / `agentmemory`, vault note `b7c168ca` "LLM Wiki v2"; also referenced in Apr 2026 batch `a1472a80` P3): Working → Episodic → Semantic → Procedural. Information is *promoted* across tiers as evidence accumulates — single observations become session summaries become cross-session facts become reusable workflows. This is more rigorous than the cognitive-science split because it's also a lifecycle.

**How Memex maps today:**

| Type | Memex substrate | Gap |
|---|---|---|
| Sensory | n/a | not applicable to text agents |
| Working | `session_id` ContextVar (`context.py:9–29`) | **per-request, not per-session** — the ContextVar is set on each request entry; there is no agent-level session that spans multiple retrieve→write turns. Any feature relying on "what was retrieved a moment ago" must use an explicit feedback API (F1's `memex_record_outcome`) rather than implicit session attribution. |
| Episodic | `Note` (timestamped, source-attributed) + ingestion sessions | retrieval has no "episode-recall" strategy in TEMPR; closest is timestamp-filtered note search |
| Semantic | `MemoryUnit` + `MentalModel` + `Entity` | well-covered — most existing infrastructure |
| Procedural — **observations only** (not the procedures themselves) | KV with `procedure:` namespace convention | The agent (Claude Code / Codex / Hermes) owns the executable procedure (its own skills system). Memex stores cross-agent observations: "For this user/project, X means Y." See F14 (revised v6); diverges from Apr 2026 batch's SKILL.md framing — see §2.3.1 below. |
| Core | absent as a first-class type | could be a "pinned" `MentalModel` or `core:*` KV namespace |
| Cross-Context | global `Entity` table (sql_models.py:701) | implicit only; no first-class lineage between sessions/projects |

**Implications for the catalog:**
- **F14 (Procedural — observations only)** is a thin KV-namespace + Hermes-briefing feature (~1 week). Memex stores observations like *"for this user, deploy means staging"* — not the deploy procedure itself. The agent owns its own skills system (Claude Code skills, Codex SKILL.md, Hermes SKILL.md); Memex storing a *parallel* skill artifact would duplicate the agent layer. See §2.3.1 for why this diverges from Apr 2026 batch's SKILL.md framing.
- **Episodic retrieval** already exists via `memex_note_search` / chunk-level `document_search.py` — the gap was conceptual clarity, not a missing mechanism. See §3.6 for the semantic vs episodic split. **F37 (originally proposed as a new TEMPR strategy)** collapses to "smarter time-bounded scoping on the existing note_search path" — small enhancement, not a new strategy. Possibly: a `time_window` parameter on note_search that biases ranking toward chunks within the requested period.
- **Working memory** gap (no real agent session) blocks any feature that wants implicit attribution. F1's explicit `memex_record_outcome` API is the workaround. Long-term, an agent-session ContextVar with explicit boundaries would unlock implicit episodic capture.
- **Core memory** is a small but real gap. Possibly a single boolean flag `is_core` on `MentalModel` plus exclusion from any deprioritization/decay logic. Add to backlog.

### 2.3.1 Why procedural memory in Memex is *observations*, not procedures

The Apr 2026 cross-system feature batch (vault note `a1472a80` P1) proposes **SKILL.md procedural memory** following Codex's pattern. After deeper consideration, this report argues the SKILL.md *artifact* belongs at the agent layer (where Codex/Hermes already put it), and Memex should store only the *observations* about how an agent's existing skills should adapt to specific contexts.

**Layer of responsibility:**

| Layer | Owns | Example |
|---|---|---|
| Agent (Claude Code, Codex, Hermes, custom) | The procedure — executable how-to, scripts, prompts, workflow logic | The "deploy" skill: tag → push → monitor |
| Memex | Observations about what works for which procedure in which context | "For this user, `deploy` means staging — never prod after 6pm" |

The agent owns the verb; Memex owns the adverb.

**Why SKILL.md inside Memex would be wrong layering:**

1. **Duplicates the agent's skill system.** Claude Code has skills (`~/.claude/skills/`). Codex has skills (`~/.codex/memories/skills/`). Hermes has skills. Each agent's loader is the canonical execution surface. Memex storing a parallel artifact creates split-brain on whose skill wins.
2. **Forces Memex to define a portable skill format and execution layer** — out of scope for a memory backend.
3. **Codex stores SKILL.md *because Codex is the agent* and has no other skills system.** The pattern doesn't transfer to a memory layer that sits behind multiple agents.

**The strong argument for keeping procedural memory in Memex at all:**

Cross-agent persistence. A user working across Claude Code + Hermes shouldn't have to teach "deploy means staging" twice. Memex stores it once; both agents query at briefing time. *This* is the value-add — not duplicating skill systems.

**Definition (commit to this):**

> Procedural memory in Memex is persistent observations about how an agent should adapt its existing skills to specific contexts (this user, this project, this codebase). It is *not* the skill itself.

**Implementation:** KV with `procedure:` prefix — convention, not new infrastructure. See F14 (revised in v6).

**Disagreement with Apr 2026 batch (`a1472a80`):** That note's "P1 SKILL.md procedural memory" is correct *as a feature for the agent layer* but wrong as a Memex feature. Recommendation: revise that batch entry to "procedural observations via KV" and leave SKILL.md to the agents.

### 2.4 Honest gap analysis vs existing Memex services

What the relevant services *actually* do today (verified in current source, not inferred from naming). Use this to avoid claiming "Memex already does X via service Y" when Y is much narrower.

| Service / module | Reality | Common misconception |
|---|---|---|
| `services/reflection.py` | **Event-driven** entity-level reflection. Process-local `asyncio.Lock` (line 55) — **NOT** distributed. `reflect()` at line 88; `class ReflectionService` at line 29. Triggered by extraction or explicit `POST /reflections` at `server/reflection.py:32`. | "Scheduler-driven" — false; it's event-driven. "Distributed-locked" — false; multi-worker uvicorn races on same entity. |
| `services/contradiction/engine.py` | `detect_contradictions(session_factory, document_id, unit_ids, vault_id)` at lines 39–45. Takes `unit_ids`, not entities. Writes `MemoryLink` records (contradicts/weakens). Batch-capable via `_process_flagged_unit`. | "Entity-scoped" — false; takes unit IDs. F9 needs an entity → unit_ids resolver. |
| `services/mental_model_cleanup.py` | **Reactive delete-cascade only.** `prune_stale_evidence` (line 21) runs when units are deleted. `cascade_chunk_unit_staling` (line 86) runs when chunks are staled. No periodic mode, no edge reweighting, no consolidation. | "Sleep consolidation equivalent" — false; only fires on deletion/staling events. |
| `services/notes.py` archive path | `set_note_status('archived')` (around `:222` valid_statuses) → `_deactivate_note_units` → `prune_stale_evidence` → can **delete** MentalModels with empty observations. **Destructive**. FileStore bytes are **not** removed by archive — only `delete_note` (`:943`) calls `txn.delete_file`. So archive is destructive on the entity graph and accumulating on disk. | "Non-destructive — just a flag." False on both axes. |
| `scheduler.py` | One global lock `MEMEX_LEADER_LOCK_ID = 5432789123456789` (line 18). `run_scheduler_with_leader_election` (line 94) acquires it and runs ALL `@clock.task`s as leader. **No per-task election.** | "Per-task advisory lock" — false; all tasks share one leader. New tasks register under the existing leader. |
| `services/kv.py` + `KVEntry` (`sql_models.py:1301`) | **Global, namespace-prefixed** (`global:`/`user:`/`project:`/`app:`). TTL via `expires_at`. Embedding-backed semantic search. **Not vault-scoped.** | "KV is per-vault" — false. Suitable substrate only for cross-vault data; wrong for vault-scoped procedures. |
| `services/entities.py` Hybrid Score | `0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality` (lines 66, 90–94). `Entity` is **global** (`sql_models.py:701`, no `vault_id`). Vault-scoping at `UnitEntity` (`:853`) and `MentalModel` (`:118`). | "Add MW counters to Entity" — breaks multi-tenancy. Counters must go on vault-scoped tables. |
| `MemoryUnit.access_count` (`sql_models.py:554`) | Column exists. **No write-site found** — appears to be dead/unwritten. | "We already track access" — only structurally; the counter never increments. |
| FSRS / spaced repetition | **Zero existing implementation.** No `next_review_at`/`review_interval_days` columns. F20 builds from scratch. | n/a |
| Eval harness | **Does not exist** (no `packages/eval/`). F23 builds from scratch. | n/a |
| Procedural memory / SKILL.md | **Zero existing implementation** (`grep -ri 'procedural\|SKILL\.md'` empty in core). F14 builds from scratch but the *pattern* is well-defined externally (Codex `d9ca8ba7`, Hermes). | "Memex already has skills" — false. |
| Hermes plugin tools | **8** Stream-1 tools: `memex_memory_search`, `memex_note_search`, `memex_survey`, `memex_add_note`, `memex_append_note`, `memex_list_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences`. Full schema ~36 across all streams. | Earlier revisions said 7 (missed `memex_append_note`). |

**What the Apr 2026 feature batch (`a1472a80`) already proposes** that this report should align with rather than re-invent:
- **P0 security** (write-time gates, junk filter, procedural firewall) — defensive against MINJA-class poisoning. **Out of scope** for this report (cognitive features only) but should be flagged as a gating prerequisite for any agent-writable curation feature.
- **P1 SKILL.md procedural memory** — F14 should align with this exact form factor.
- **P1 query routing with abstention** — orthogonal to this report; flag as adjacent work.
- **P3 usage-based retention** (Codex pattern: `usage_count` + `last_usage` + `max_unused_days`) — should be folded into F11 (decay) rather than treated as separate.
- **P3 4-tier consolidation hierarchy** — informs §2.3 (already added above).
- **Prior proposal `edbe3e4b`** (LLM Wiki / MemPalace lessons) is the predecessor of much of this; cross-reference.

These principles govern the catalog. Every feature inherits them unless explicitly noted.

### 3.1 Non-destructive curation by default

KinthAI's principle (#64 comment): *"forgetting is not about deleting data — it is about adjusting retrieval weights."* This report adopts that as the architectural commitment. Every curation mechanism must operate by *adjusting retrieval weights* rather than removing data. The thesis is foundational; §3.4 below explains how multiple signal sources compose into the resulting weight.

If an operation can be *undone by recomputing or flipping a single flag with no side effects*, the agent does it autonomously. If it triggers cascading deletes or its only undo is restoring from backup, a human approves first.

**Operation taxonomy:**

| Operation class | Examples | Reversible? | Gate |
|---|---|---|---|
| Weight adjustment | MW counter increment, MW score recalculation | Yes (recompute) | Agent autonomous |
| Deprioritize (NEW flag) | Set `MemoryUnit.is_deprioritized = TRUE` | Yes (flip back) | Agent autonomous |
| Mark stale (existing) | `MemoryUnit.status = STALE` via `_deactivate_note_units` → cascades to `prune_stale_evidence` for both `supersede` and `archive`. May delete empty `MentalModel`s whose evidence becomes empty. **This is correct by design**: the synthesis is no longer epistemically valid once its evidence is gone. The non-destructive complement is `is_deprioritized` (see Deprioritize row above). | **Destructive on graph** — deleted MentalModels would need re-reflection to restore from re-ingested evidence. | Agent autonomous for single-unit case (auditable, restorable via re-ingestion); human-gate for bulk |
| Structural correction | Merge proposed duplicate entities (with audit) | Yes (audit-trail rollback) | Agent autonomous |
| Archive — single note (current behavior; **destructive on entity graph by design**) | `Note.status = 'archived'` via `set_note_status` (`services/notes.py:222`) → `_deactivate_note_units` → marks units stale AND calls `prune_stale_evidence`. May delete empty MentalModels. The note row + FileStore bytes are preserved (lifecycle flag, not row deletion); the *graph* synthesis is cleaned up. **F4's `memory_deprioritize` is the non-destructive verb** — use it when the intent is "lower the weight, keep the synthesis." | Note row reversible (`status` flip back to `active`); deleted MentalModels are **not** auto-restored — restoration path is re-reflection over re-ingested evidence. | **Agent autonomous** for single-note archive — auditable; restoration path documented; bulk-archive gating below mitigates the runaway-deletion risk |
| Archive — vault-wide / bulk (≥10 notes in one operation) | Same primitive as single-note archive, just looped. Because each archive cascades to `prune_stale_evidence`, a misapplied bulk operation can vaporize a substantial fraction of a vault's synthesized models in one call — and re-reflecting the surviving evidence is non-trivial. The damage isn't undone by flipping note status back. | Note rows reversible per-note; mental-model damage requires bulk re-reflection | **Human required** — the agent must surface the affected note count + vault + estimated MentalModel impact before executing |
| Hard delete | GDPR right-to-be-forgotten | No | **Human required** |
| Bulk structural | Vault-wide schema migration | Effectively no | **Human required** |

**Retrieval scope matrix — `is_deprioritized` is orthogonal to `status`** (they compose independently in the WHERE clause). v6.9 uses the existing `include_stale` kwarg (already in `apply_generic_filters`) for the status axis; F1b adds **only** `include_deprioritized` as a new kwarg:

| `include_deprioritized` | `include_stale` | Effective WHERE clause | Use case |
|---|---|---|---|
| `false` (default) | `false` (default) | `status = ACTIVE AND is_deprioritized = false` | Most queries — the agent's working scope |
| `true` | `false` | `status = ACTIVE AND TRUE` | Explicit recall: *"remember when..."*, direct references to past discussions |
| `false` | `true` | `status IN (ACTIVE, STALE) AND is_deprioritized = false` | Re-evaluating units linked to recently archived/superseded notes |
| `true` | `true` | `status IN (ACTIVE, STALE) AND TRUE` | Historical archaeology / debugging the lifecycle pipeline |

Two columns, two flags (one new — `include_deprioritized`; one existing — `include_stale`), four combinations — no enum gymnastics, no migration of `ContentStatus`, no new `include_archived` flag. Archived notes' units land in the STALE bucket (the existing `include_stale` flag governs visibility); the cascade through `prune_stale_evidence` may also delete MentalModels with no remaining evidence (intentional — see §3.1 Archive row and §6 #12). F1b's parameterization is one branch in `apply_generic_filters` — see F1b.

### 3.2 Separate write-time judgment from retrieval-time scoring

- **Write-time** = intent (Permanent/Durable/Ephemeral), risk class (none/sensitive/private), importance estimate. Set once at extraction; rarely changed.
- **Retrieval-time** = MW score, importance-weighted decay, recency boost, reranker score, MMR diversity. Computed per query and **composed at the reranker stage** (see §3.4).

These are complementary, not redundant. F25 (write-time classifier) feeds F11 (importance-weighted decay) and provides the intent/importance values that decay rates depend on; F1 (MW counters) drives the behavioral side. Both then meet at the reranker — see §3.4 for the composition model.

### 3.3 Memory must be observable

Every new mechanism (MW counters, lint findings, deprioritization decisions, intent classifications) emits Prometheus metrics and OpenTelemetry traces. Diagnostics CLI/UI (F32) makes the memory structure visualizable. The user + daily reflection cron + occasional spot-checks are the quality signal at personal scale (formal multi-judge eval harness — F23 — was demoted in v6.7; reconsider only at multi-tenant scale).

### 3.4 Retrieval-weight composition: one principle, many signals

This composition governs **`memex_memory_search`** — the primary retrieval surface that handles semantic, temporal, and entity-scoped queries over MemoryUnits. `memex_note_search` (chunk-level source drill-down) doesn't apply this composition directly but exposes unit status as traversal metadata — see §3.6 for that pattern.

KinthAI's principle (§3.1), Memory Worth (F1), and FSFM importance-weighted decay (F11) are **the same thesis** — continuous, non-destructive retrieval-weight adjustment. They are *not* competing features. They are orthogonal **signal sources** that compose into a single downstream computation: the reranker's multiplicative weight chain.

**The relationship:**

| Layer | What | Role |
|---|---|---|
| **KinthAI principle** | "Forgetting is not deletion — it is weight adjustment" | The architectural commitment (§3.1) |
| **MW (F1)** | `success_co_count / (success+failure)` → soft-factor at reranker | Behavioral / episode-level signal |
| **FSFM importance-weighted decay (F11)** | `importance × exp(-elapsed/stability)` → factor at reranker | Temporal × content-intrinsic signal |
| **MW exploration floor (F33)** | ε-greedy injection of low-MW units | Counter-bias against rich-get-richer dominance of behavioral signal |
| **Existing recency × temporal boost** | `engine.py:1132–1140` | Already-shipped temporal signal |

**Why orthogonal, not redundant:**
- MW catches *"this fresh memory just led to a failure"* — behavioral, no time component
- FSFM catches *"this 3-year-old preference is stale because the user changed jobs"* — temporal, no behavioral component
- Recency catches *"this is fresh, prefer it for ambiguous queries"* — pure time
- Each signal is blind to what the others see. They compose multiplicatively at the reranker.

**The composition site and pipeline ordering** (resolves Appendix B M1; refined v6.9 against `WAVE-0-PREWORK.md` §4.1):

The reranker stage at `engine.py:1140` (the boost line — not 1116, refined per Wave 0 verification) is the single composition site. The full retrieval pipeline order is:

1. Query expansion + embeddings
2. RRF fusion (in DB, upstream — overwritten by reranker)
3. Hydration + superseded-threshold filter (post-hydration confidence cutoff)
4. **Reranker stage** — cross-encoder scores → sigmoid-normalized → `ce_score × recency_boost × temporal_boost` at `engine.py:1140`. This is where new signals attach as **additive-marginal boosts** (see formula below).
5. Position-aware blending (optional)
6. MMR diversity (`engine.py:469-504`, calling `_apply_mmr_diversity` at `:1227`) — operates on the composed score from step 4

Currently:
```python
# engine.py:1140
boosted_scores.append(ce_score * recency_boost * temporal_boost)
```

After F1c (additive-marginal pattern, matching the existing `recency_boost = 1.0 + alpha*(recency-0.5)` shape):
```python
mw_score = beta_bernoulli_mean(success_co_count, failure_co_count, alpha=1, beta=1)
mw_boost = 1.0 + mw_alpha * (mw_score - 0.5)   # mw_alpha default 0.3
boosted_scores.append(ce_score * recency_boost * temporal_boost * mw_boost)
```

After F11: an analogous `importance_decay_boost` joins the same chain.

**Why additive-marginal, not pure multiplicative sigmoid** (refined v6.9): a pure `× sigmoid(MW − threshold)` would zero out cold-start units (`MW = 0/0` → `sigmoid(−threshold) ≈ 0.1`). The additive-marginal pattern with α=β=1 prior gives `mw_score = 0.5` → `mw_boost = 1.0` for new units (perfectly neutral), so cold-start has no penalty. As outcome data accumulates, `mw_score` moves away from 0.5 and the boost compounds.

MMR is **strictly downstream** of every signal in the composition. New signals never bypass the reranker site, and MMR's λ never has to be re-tuned for individual signals because it sees only the composed score. This is the property the M1 finding asked us to make explicit.

**Architectural implications:**

1. **Don't build separate "decay" and "MW" pipelines.** They share the same composition site (`engine.py:1116`) and the same downstream MMR re-ordering. A single "reranker weight composition" code path takes all signal contributors as inputs.
2. **Each signal needs its own data plumbing** — outcome API for MW, decay computation for FSFM, etc. — but the *application* of all signals is one piece of code.
3. **Add new signals here, not elsewhere.** When future research surfaces another signal (e.g., contradiction-strength, source-trust), it joins the composition rather than creating its own ranking layer.
4. **Quality signal is the composed weight, not individual signals in isolation.** Per-signal ablations are diagnostic; the production "is this working?" check is composed retrieval quality, observed via the reflection cron + spot-checks (F23 formal harness deprioritized v6.7).

**Why F1 and F11 are still separate features in the catalog:**
- Each requires distinct upstream data (outcome API vs decay computation vs importance scoring)
- Each can be developed and validated independently before being added to the composition
- Tier placement reflects *evidence strength for that signal source*, not architectural separation

But they share a single composition site. Treating them as independent ranking layers would re-introduce the "compete for primacy" problem KinthAI's principle is designed to avoid.

### 3.4.1 Memory Worth: how it lifts retrieval quality

§3.4 explains *where* MW joins the composition and *with what shape* (`mw_boost = 1.0 + mw_alpha * (mw_score − 0.5)`). This subsection explains *why this is a quality lift, not just a rearrangement* — what blind spot in pure semantic retrieval MW closes, and why adding it doesn't introduce a worse failure mode.

**The blind spot MW closes.**

Without behavioral feedback, retrieval ranks by content match (cross-encoder), recency, and temporal boosts. Cosine similarity is structurally blind to *whether a memory has actually helped when retrieved*. A unit can be:

- Semantically perfect for the query,
- Recent enough to clear temporal cutoffs,
- And still consistently useless — because it was a stale workaround, a fixed bug that keeps resurfacing, an old preference the user has revoked, or a fact that was already wrong when written.

Pure similarity has no mechanism to learn from past retrieval outcomes. Every retrieval episode is independent: the system can't tell that *this exact unit* keeps surfacing for *this kind of query* and *never leads to useful action*. MW is the channel through which retrieval episodes feed back into ranking.

**The signal.**

Each `MemoryUnit` carries Bernoulli counters (`success_co_count`, `failure_co_count`) updated by `memex_record_outcome(unit_ids, success)`. Every retrieval episode that gets a verdict — agent acted on the recall usefully (or didn't) — adjusts the counters.

```python
mw_score = beta_bernoulli_mean(success_co_count, failure_co_count, alpha=1, beta=1)
```

The α=β=1 prior is the load-bearing detail: a unit with `0/0` counters scores **exactly 0.5**, which through the additive-marginal boost yields `mw_boost = 1.0` — perfectly neutral. Cold-start units pay no penalty. This is the KinthAI principle in action: new memories don't have to "earn their place" against established ones; they ride the cross-encoder until enough outcome data accumulates.

**The concrete quality lift.**

Two units tie on cross-encoder score for the query *"dependency injection setup"*:

| Unit | Outcomes | mw_score | mw_boost (α=0.3) | Effect on rank |
|------|----------|----------|------------------|----------------|
| A | 8 success / 0 failure | ~0.9 | 1.12 | +12% above semantic baseline |
| B | 1 success / 7 failure | ~0.22 | 0.91 | −9% below baseline |
| C | 0 success / 0 failure (new) | 0.5 | 1.0 | unchanged — rides ce_score |

A ranks above B even though the cross-encoder couldn't tell them apart. C — brand new — competes on equal footing with the cross-encoder ranking it had before MW was introduced.

After enough turns, behaviorally-useful memories accumulate above behaviorally-stale ones, **without anyone explicitly curating**. That's the quality lift: a free axis of ranking improvement that piggybacks on retrievals the system was running anyway.

**Why MW doesn't degenerate into rich-get-richer.**

The obvious failure mode of any feedback-driven ranking is a runaway loop: high-MW units keep getting boosted → keep being retrieved → keep accumulating success → keep being boosted further. Failed units never get a second chance to prove themselves on a different query. The ranking calcifies.

**F33 exploration floor** is the structural counter-pressure (§4 Tier S). It ε-greedily injects low-MW units into result sets — bypassing the reranker boost. A unit with `mw_score = 0.2` will occasionally surface anyway. The user/agent gets to re-validate:

- If still useless → another `record_outcome(success=false)` compounds the penalty (it sinks further, still surfaces less often).
- If now useful → `success=true` recovers the score (the unit climbs back into normal ranking).

This is the property that makes MW + F33 a *learned* ranking signal rather than a *frozen* one. F33 isn't a nice-to-have — without it, MW becomes monotonic and untrustworthy. They were planned together; they ship together (Tier S Wave 1).

**Why MW is orthogonal to recency, FSFM, and similarity (not redundant).**

Each signal sees a dimension the others are blind to:

- **MW** — *"this fresh memory just led to a failure"* (behavioral, no time component)
- **FSFM** — *"this 3-year-old preference is stale because the user changed jobs"* (temporal × content-intrinsic; behavioral-blind)
- **Recency** — *"this is fresh, prefer it for ambiguous queries"* (pure time)
- **Cross-encoder** — *"this matches the query semantics"* (content match, history-blind)

A unit that's stale by FSFM, recent by recency, low-MW by behavior, and a perfect semantic match would land *somewhere reasonable* in the composed score because each signal pulls it in a different direction and they multiply at the reranker. No single signal dominates; no signal is silently overruled. That's the §3.4 architectural principle made concrete: MW earns its place by showing up where the others are blind, not by competing with them for the same axis.

**MW is the gradient; deprioritize is the binary** (the §3.5 user-confirmed-fix flow leans on this).

MW changes the score by single-digit percentages per outcome. It needs many turns to make a unit invisible. That's the right shape for *most* feedback — small, accumulative, reversible by F33 re-validation. But sometimes the user has a definitive verdict (*"this bug is fixed; never surface it again"*). For that, F4's `memory_deprioritize` is the binary hard exclusion. They work together: deprioritize handles user verdicts; MW handles the slow drift the user never explicitly marked. §3.5 describes how the user-confirmed-fix flow uses both because it's both signal types at once.

**What MW does not solve.**

- **Cold-start retrieval quality**: MW is neutral until outcomes accumulate. The first weeks of using a fresh vault rank purely on cross-encoder + recency + temporal — same as a system without MW.
- **Outcome attribution at scale**: every retrieval episode that updates MW has to *get a verdict from somewhere*. The agent must call `record_outcome` reliably. Missing outcomes is the silent failure mode — MW becomes biased toward whichever paths happen to record. Mitigated by writing outcome calls into the retrieval skill (so it's hard to forget), and by F33 re-validation cycling through low-confidence units.
- **Bad-faith outcome data**: if outcomes are recorded incorrectly (programmatic bug, agent misjudgment), MW learns the wrong thing. Mitigated by the audit log on `record_outcome` (every write traceable) and the reversibility property (F33 + counter-evidence can pull a unit back up).

These are operational cautions, not architectural defects. MW's quality contribution stands as long as outcome data is reasonably representative.

**Compute cost considerations: pre-reranker filtering as a Tier A optimization.**

§3.4 presents MW and FSFM as *post-reranker* multipliers — composing at `engine.py:1140` after the cross-encoder has already scored every candidate. That preserves architectural simplicity (one composition site) and keeps signals reversible (no hard cutoffs, no cold-start penalty, F33 can recover). It also means the cross-encoder runs on candidates that get heavily downweighted by the post-reranker boosts.

**Measured cost on the production deployment** (May 2026):

- Hard cap on candidates handed to the reranker: **70**
- Reranker latency at that cap: **~1.5 s** (cross-encoder, GPU-batched)
- Per-candidate cost: ~21 ms

If a meaningful fraction of those 70 candidates are deeply low-MW or deeply low-FSFM (units the user has effectively abandoned, fixed bugs that semantic-similarity still loves), the cross-encoder is paying full cost on units that will get multiplied by ≤ 0.85 afterwards. **Empirically observed worst case**: 30 of 70 candidates downweighted past usefulness → ~640 ms of reranker compute spent on units that don't make the returned set. That's > 40% of reranker budget.

At the original "<100 ms reranker, optimization is noise" assumption (an earlier estimate this report carried) the architectural simplicity argument wins. **At 1.5 s and 40% waste, it doesn't** — pre-filtering deeply-failed units before the cross-encoder is justified by latency, not just an aesthetic preference. This promotes pre-reranker filtering from "Tier B with a measurement gate" to a **Tier A architectural recommendation**.

**The guarded pre-filter design** (slots between hydration step 3 and reranker step 4 in the §3.4 pipeline). Two orthogonal signals, **OR'd** together — either condition is sufficient to skip the cross-encoder:

```sql
-- Applied during candidate selection, before cross-encoder forward passes
WHERE NOT (
    -- MW branch: behavioral failure (ships in F40)
    (
        (success_co_count + failure_co_count) >= 5    -- enough evidence to act on
        AND mw_score < 0.15                            -- strongly failed
    )
    OR
    -- FSFM branch: temporal/importance decay (ships when F11 lands; no-op until then)
    (
        importance * exp(
            -EXTRACT(EPOCH FROM (now() - last_outcome_at)) / 86400.0 / NULLIF(stability, 0)
        ) < 0.10
    )
)
-- AND lifecycle filters (already there)
-- AND is_deprioritized = false (already there)
```

**Three safeguards keep the §3.4 composition invariants intact:**

1. **MW branch protects cold-start via the evidence threshold.** A unit with 0 outcomes (`mw_score = 0.5`) has zero evidence to act on, so the `>= 5 outcomes` clause keeps it in the candidate set. Cross-encoder ranks it on content alone. Same protection the additive-marginal MW boost provides at the post-reranker stage.
2. **FSFM branch protects cold-start via the elapsed-time term.** A fresh unit has `last_outcome_at = now()` → `elapsed = 0` → `exp(0) = 1` → score = `importance × 1` ≈ neutral or high (importance is bounded ≥ a floor by F25). Decay only erodes the score after meaningful time passes; new units are never pruned.
   - **NULL-handling cold-start commitment**: when `stability` or `importance` is NULL on unclassified units (pre-F25/F11), the FSFM predicate evaluates to NULL → treated FALSE in WHERE → unit is kept (cold-start safe by design). Implementations MUST NOT add `COALESCE(stability, <default>)` or `COALESCE(importance, <default>)` — that would inadvertently filter cold-start units. Tests must assert NULL inputs propagate through to "kept".
3. **F33 exploration runs on a separate retrieval path that bypasses this filter.** The whole point of F33 is to occasionally re-validate low-MW (and low-FSFM) units; if the pre-filter blocks them, the system loses its self-correction property. Implementation: F33's candidate fetch issues a separate query without the pre-filter, marks results as exploration-injected, and reuses MMR for diversity at the merge.

**Why OR'd, not AND'd.** A unit can be behaviorally failed but recent (low MW, fresh) — MW branch prunes it. A unit can be temporally stale but never retrieved (low FSFM, neutral MW with `< 5` outcomes) — FSFM branch prunes it. Either reason is sufficient grounds to skip the cross-encoder; requiring both would underprune.

**Sequencing.** F40 (MW branch) ships first as Tier A — claims the ~30% latency win immediately. The FSFM clause is a no-op placeholder until F11 (FSFM-lite decay scoring, currently Tier B) ships and adds the `stability` and `last_outcome_at` columns + the importance signal from F25. When F11 lands, the FSFM clause activates with no breaking change to F40.

**FSFM branch ships inert at F40 ship time.** Concretely: the SQL clause is gated by a feature flag (`fsfm_branch_enabled: bool = False` on `RetrievalConfig`), default OFF. F11 ships the column migration (`stability`, `importance`) AND flips the default ON in the same PR. Until F11, the WHERE-clause builder skips the FSFM branch entirely — no SQL referencing missing columns is emitted, so F40 cannot break a vault that hasn't yet had F11's migration applied.

**Compute model: on-the-fly at hydration, NOT precomputed columns.**

Question worth answering up front: should `mw_score` and the FSFM-decayed score be **stored as columns** (precomputed, indexed, maintained by triggers) or **computed in the WHERE clause** at query time?

**Recommendation: compute on-the-fly at hydration time.** Reasoning:

| Concern | Precompute | On-the-fly | Winner |
|---|---|---|---|
| **Where filter actually runs** | Whole-vault SELECT at filter time, would need an index to be fast | ~200-row hydration candidate set already narrowed by RRF — `exp()` per row is <1 ms total | On-the-fly |
| **FSFM depends on `now()`** | Requires periodic refresh (e.g., hourly cron) → score is stale; or recompute on read → degenerates to on-the-fly anyway | Always reflects current time, atomic with the read | On-the-fly |
| **Maintenance overhead** | Migration adds `mw_score` column + trigger on every `record_outcome` write + reconciliation handler if drift appears | Zero — derived from existing counters via a SQL expression | On-the-fly |
| **Drift risk** | Stored score can disagree with raw counters under partial failure / out-of-order writes; needs reconciliation | None — always derived | On-the-fly |
| **Indexability** | `mw_score` could be btree-indexed; FSFM cannot (now() is volatile) | Predicate is non-sargable but only evaluates over the ~200-row hydration set, which is a hash/index lookup by ID anyway | Tie at this scale |

**The load-bearing fact: the pre-filter doesn't run on the whole vault. It runs on the hydration candidate set produced by RRF — typically ~200 rows.** At that scale, postgres evaluates the expression in well under 1 ms. Precomputing and maintaining a column for sub-millisecond gain isn't worth the migration + trigger + drift surface.

**Concrete implementation:**
- Integrate the `WHERE NOT (...)` clause into the **hydration query** (the SELECT that fetches full unit bodies for the RRF top-K). The query already filters by `unit_id IN (...)`; add the MW + FSFM predicate alongside.
- `mw_score` — derive inline as a SQL expression: `(success_co_count + 1.0) / (success_co_count + failure_co_count + 2.0)` (Beta-Bernoulli α=β=1 closed form). Same formula `compute_mw_score()` uses in Python; no helper function needed at the SQL layer.
- `stability` and `last_outcome_at` — both already required columns by F20 (FSRS) and F1a respectively. No new schema for the FSFM branch beyond what F11 adds.

**When precomputation would become justified** (re-evaluate, don't pre-build):
- If profiling shows hydration-time evaluation > 5 ms p95 over a representative workload (unlikely at 200 rows; more likely if the hard cap rises to 500+ or the vault has unusual MW score distributions).
- If FSFM eventually surfaces in user-facing query paths beyond pre-filter (e.g., a "show me my decayed memories" diagnostic) — at that point a maintained column with periodic refresh is the right tradeoff.

Until those conditions trigger: **on-the-fly. F40 ships with no schema change.**

**Expected savings.** If pre-filter eliminates ~25–30% of candidates that would have been deeply downweighted anyway, reranker drops from 70 to ~50 candidates → **~1.05 s instead of ~1.5 s** (~30% latency win), with no quality loss for the obviously-failed tail. F33-injected units bypass the filter, so reversibility is preserved.

**Other latency levers worth catalog entries** (for the same Tier A optimization arc):

- **Cross-encoder score cache** keyed on `(query_embedding_hash, unit_id)` with TTL (e.g., 24h). Repeat queries get free reranking; biggest single lever for narrow query patterns (a daily "morning briefing" loop hits it hard).
- **Smaller reranker model** if quality tolerates — biggest absolute saving but quality regression risk; gate behind an A/B with a recall-impact metric.
- **Quantize / int8-batch the cross-encoder** if not already done — typically 2× speedup without recall loss.

These are pre-reranker (filter), per-query (cache), and intra-reranker (model size, quantization) optimizations respectively. They compose. Sequence the pre-filter first because it requires no ML work and the guardrails are well-defined.

**Summary.** MW lifts retrieval quality by adding a behavioral axis to a previously content-and-time-only ranking, without penalizing cold-start units, without replacing the cross-encoder, and self-correcting via F33 against rich-get-richer. It's the signal that lets the system learn from its own retrieval history — which pure similarity-based systems structurally cannot do. At measured production reranker latency (~1.5 s @ 70 candidates), a guarded pre-reranker MW filter is a **Tier A optimization**, not a Tier B defer — it preserves §3.4 composition invariants while reclaiming ~30% of reranker compute.

### 3.4.2 Contradiction-derived confidence: completing the reranker composition

§3.4 / §3.4.1 establish the principle: behavioral and structural ranking signals (MW, recency, temporal proximity) compose at the same site, as multiplicative boosts on the cross-encoder score, with cold-start neutrality and observable per-signal contribution. **Contradiction is missing from this composition** — and the gap is invisible at the source level.

**The current state.**

The contradiction engine (`packages/core/src/memex_core/memory/contradiction/engine.py:211-219`) adjusts `MemoryUnit.confidence` when contradictions are detected:

| Relation | Confidence step | Default α |
|---|---|---|
| reinforce | `confidence + α` (capped at 1.0) | 0.1 |
| weaken | `confidence − α` (floored at 0.0) | 0.1 |
| contradict | `confidence − 2α` (floored at 0.0) | 0.1 |

A unit starts at `confidence = 1.0` and walks toward 0.0 as evidence against it accumulates. So far, so symmetric with MW: an evidence-accumulating Bayesian-flavored counter on the unit.

The asymmetry shows up at the reranker:

```python
# packages/core/src/memex_core/memory/retrieval/engine.py:1195
final = ce_score × recency_boost × temporal_boost × mw_boost
```

`confidence` is **not in the composition**. The hydration step (engine.py:1051-1091) loads `superseded_by` metadata for `confidence < 1.0` units, but that's a *display* field — surfaced to agents/UI for context — not a ranking input. A contradicted unit and a non-contradicted unit, all else equal, get the same final reranker score today. The "downranking from contradiction" most readers assume is happening is folklore, not code.

There is also no `[CONTRADICTED]` text label injected into the cross-encoder input (`packages/core/src/memex_core/memory/formatting.py:format_for_reranking` builds only the date prefix and type marker). Even if there were, it would be the wrong shape: the cross-encoder wasn't trained on Memex's marker; it would be hallucinating a meaning. Behavioral and structural signals belong as their own composition axis, not folded into the cross-encoder's text input.

**The fix: post-reranker confidence boost (F47).**

Add a fourth multiplicative boost at the same site:

```python
confidence_boost = 1.0 + confidence_alpha × (unit.confidence − 0.5)
final = ce_score × recency_boost × temporal_boost × mw_boost × confidence_boost
```

| Confidence | confidence_boost (α=0.3) | Effect on rank |
|---|---|---|
| 1.0 (never contradicted, the schema default) | 1.15 | mild lift; mirrors MW's "high-evidence success" case |
| 0.9 (one weakening event) | 1.12 | near-neutral lift |
| 0.8 (one contradiction event, default α) | 1.09 | small penalty relative to clean units |
| 0.5 | 1.00 | exactly neutral |
| 0.2 | 0.91 | substantial penalty |
| 0.0 | 0.85 | floor penalty |

`confidence_alpha` is a config field on `RetrievalConfig`, analogous to `reranking_mw_alpha`. **Ship default: `confidence_alpha = 0.0` (off)** — flip to a non-zero value (target ~0.3 for parity with MW) only after calibration data accumulates, mirroring F1c's `mw_alpha` start-after-counters-populate convention. Reason: with `confidence = 1.0` as the schema default, any non-zero α at ship time gives every never-contradicted unit a multiplicative lift (the table above shows +15% at α=0.3) — most units get the boost, distorting the score distribution before the calibration that would justify it. The illustrative table is kept at α=0.3 because it reads cleanest at that value; treat it as the post-calibration target, not the shipping default.

Observability: `CONFIDENCE_BOOST_OBSERVED` Prometheus histogram analogous to `MW_BOOST_OBSERVED` (engine.py:1162). Per-unit logged contributions stay readable: `ce=0.84 × recency=1.05 × temporal=1.00 × mw=1.08 × confidence=0.91 → final=0.86`.

**Composition variance bounds.**

Post-F47 the reranker composition becomes `final = ce_score × recency_boost × temporal_boost × mw_boost × confidence_boost` — four multiplicative boost factors on top of `ce_score`. With α=0.3 on each, the boost-only dynamic range is roughly `0.85⁴ ≈ 0.52` → `1.15⁴ ≈ 1.75`, a ~3.4× compounded range. Once F11's `importance_decay_boost` joins the chain (same composition site), this becomes five factors and the range widens further. Practical implication: MMR diversity λ may need re-tuning as additional boost factors land — flag this as an F11/F47 follow-up checkpoint to verify the diversity penalty still gates appropriately against the widened score distribution. No spec-level clamp is added now (YAGNI) — the boosts are intentionally additive-marginal and α-tunable, so observed behavior on a representative workload is the right gate. If before/after benchmarks show score-distribution compression (top-K candidates piling near the same final score so MMR can't differentiate), a `max_boost_compound` clamp on the product of boost factors can be added in a follow-up ticket.

**The fix: pre-reranker confidence filter (F48).**

Confidence < 0.2 means the unit has accumulated multiple contradiction events (≥5 contradicts at default α, or ≥9 weakens). Paying full reranker cost on such units only to multiply them down by `confidence_boost ≈ 0.91` is wasteful. F48 adds a third OR'd branch to F40's predicate:

```sql
WHERE NOT (
    -- MW branch (F40)
    ((success_co_count + failure_co_count) >= 5 AND mw_score < 0.15)
    OR
    -- FSFM branch (F40, no-op until F11 ships)
    (importance × exp(-elapsed/stability) < 0.10)
    OR
    -- Confidence branch (F48)
    confidence < 0.2
)
```

No separate evidence-amount threshold for the confidence branch — the contradiction engine's α-stepping already requires multiple events to reach the floor (default α=0.1 means at least five `contradict` events or nine `weaken` events). Adding a count threshold would double-count.

**Pre-filter reversibility — single `apply_pre_filter` flag.**

The pre-filter raises an architectural concern: what if the user is *looking for* contradicted memories? Auditing how a belief evolved, reconstructing a historical timeline, or asking "what did I used to think about X" — these queries want the contradicted units, not the current authoritative ones.

The bypass surface is one parameter on `RetrievalRequest`:

```python
class RetrievalRequest:
    ...
    apply_pre_filter: bool = True  # default-on; off for historical/audit queries
```

When `False`, the entire `WHERE NOT (...)` clause drops out — every branch (MW + FSFM + confidence) is bypassed in one go. Plumbed via `apply_generic_filters`, the same site F1b uses for scope flags.

Single flag — not three — because the three filter signals share one cognitive model: *"things the system normally hides because they're low-quality / decayed / contradicted."* The user/agent isn't reasoning about MW vs FSFM vs confidence independently; they're toggling between "give me what's relevant *now*" and "show me the hidden stuff". One flag matches the mental model. F43 codification then teaches one rule: historical / audit / lineage queries → `apply_pre_filter=False`.

A counter-argument exists: granular flags would let a power user say *"low-MW units yes, but skip stale ones."* This case is theoretical; nobody phrases queries that way. If it ever appears, mitigate by `apply_pre_filter=False` + bumping `top_k` + post-filtering in agent code. YAGNI for the granular API surface.

Note that the post-reranker boost (F47 confidence_boost) **still applies** when `apply_pre_filter=False` — i.e., contradicted units appear alongside clean ones, but rank below them. That's the right ordering for audit queries: show me everything, with the contradicted material naturally weighted lower so the current authoritative version is at the top.

**Dedicated path: contradiction-graph traversal for timeline queries (F49).**

`memex_memory_search(apply_pre_filter=False)` returns a *ranked semantic set*. For a query like *"how has my view on X evolved"*, walking the contradiction graph directly gives cleaner semantics — an *ordered chain* in temporal order, not a similarity-ranked set.

The contradiction engine already creates `MemoryLink` rows with `link_type IN ('contradicts', 'weakens', 'reinforces')` and metadata pointing at authoritative + superseded units (`packages/core/src/memex_core/memory/contradiction/engine.py:225-235`). The graph is explicit; F49 is a tool that walks it.

```
memex_get_unit_history(unit_id, max_depth=10)
```

Starts at a unit, walks backward via incoming `contradicts` / `weakens` links, returns `(predecessor_unit, link_type, link_metadata.reasoning, timestamp)` per hop in chronological order. No reranker, no boosts, no quality filtering — graph walk is for completeness, not relevance. Cycles capped by `max_depth`; branching predecessors return as parallel chains. *`reinforces` links are excluded from the default backward traversal because they point forward in time (a newer unit reinforcing an older one) — walking them backward inverts the timeline. A future extension can add a `forward=True` mode that walks `reinforces` separately, but the v1 timeline is strict supersession history.*

`confidence_boost` is **not** applied — confidence is itself the artifact the timeline is exploring, so multiplying by it would be circular.

**Composition with §3.4.1's MW story.**

F47 (boost) + F48 (filter) follow the exact pattern established for MW: F1c (post-reranker boost) + F40 (pre-reranker filter), both extending §3.4's KinthAI principle. The Memory Worth paper (2604.12007v1, §3.4 cross-ref in §2.1) is the precedent: behavioral/structural signals belong as their own composition axis with their own evidence-amount semantics. F47/F48 extend this from outcome counters to contradiction-derived confidence — same pattern, different evidence type. Theorem 4.1's almost-sure-convergence caveat noted there applies here too: do *not* claim convergence guarantees for `confidence_boost` either; under non-stationary multi-tenant ingestion, it's a useful Bayesian-flavored ranking signal in practice, not a provably-convergent one.

**Tradeoffs to validate before flipping defaults.**

- **α calibration risk**: too aggressive → buries genuinely-contradictory observations the user wants to see. Too mild → no behavioral change. Pin to a benchmark query set; mirror F1c's calibration approach.
- **Behavioral change**: existing retrieval results will shift (some currently-surfaced contradicted units drop). Run before/after on a representative workload before promoting `confidence_alpha` and the F48 filter to default-on.
- **Double-counting check**: confirm `confidence` isn't already silently filtering elsewhere. Engine.py:1052 only loads supersession metadata for `confidence < 1.0` — a display path, not a candidate filter, so no double-count on the main hydration query. Verify no other site filters on confidence before promoting F47/F48 to default-on.
- **F33 reversibility**: F44 (F33's bypass path for F40) automatically extends to F48 — F33's separate retrieval query omits the entire `WHERE NOT (...)` clause, so contradicted units re-surface on exploration cycles even with F48 active. The user gets to re-validate; another `record_outcome(success=false)` or another contradiction event compounds the penalty. Reversibility is preserved.

**Sequencing.**

1. **F47 first** — validates that confidence is actually a useful ranking signal at the post-reranker stage. Low risk: cold-start units stay neutral; tunable via `confidence_alpha`.
2. **F48 next** — once F47's `CONFIDENCE_BOOST_OBSERVED` histogram confirms that low-confidence units are being multiplied down by ≥50%, filter them pre-reranker. Latency win is marginal (5–10% on top of F40), but the architectural symmetry is the main argument.
3. **F49 independent** — graph-walk timeline can ship anytime once F47 establishes confidence as a first-class signal worth surfacing in dedicated tools.

**Summary.** The reranker composition site is the canonical home for behavioral and structural signals. MW (F1c) and contradiction-derived confidence (F47) share that home and follow the same shape — multiplicative additive-marginal boosts, cold-start neutrality, evidence-amount semantics, observable per-unit contribution. The pre-reranker filter (F40 + F48) shares one bypass flag because the user-facing question is binary: *"show me what's relevant now"* or *"show me what's normally hidden."* Audit queries, timeline reconstructions, and lineage walks route to the bypass path or to F49's dedicated graph traversal. Adding F47/F48/F49 closes the asymmetry that left contradiction silently invisible to ranking despite the contradiction engine doing real work to track it.

### 3.5 Worked example: cron reflection → user-driven resolution (handled by existing primitives)

The user runs a daily reflection cron that posts to Telegram. A typical morning output identifies real issues (GitHub MCP regression, Telegram media handling, etc.). By afternoon all three issues have been fixed. The user says, in chat:

> *"The GitHub MCP and Telegram issues from yesterday's reflection are all fixed."*

**What happens with only existing plan primitives (F1 + F4):**

The agent walks five steps. None require new features; each step uses an existing primitive.

**Step 1 — Disambiguate first, then resolve to candidate scope.**

If the user's claim is ambiguous (multiple candidate notes, multiple candidate topics, or topic that may span notes), the agent **must ask** before writing. Examples that warrant a clarification turn:
- *"telegram issues from yesterday"* when there are three reflection notes from the last week mentioning Telegram
- *"the auth bug we discussed"* with no temporal anchor
- multiple distinct issues conflated into one statement (*"the issues are fixed"* — which ones?)

Once the user has named a concrete scope, the agent picks the cheapest retrieval path that matches what they've said:

| Information user supplied | Right tool |
|---|---|
| Title fragment ("yesterday's reflection", a note title the agent already knows) | `memex_find_note(query="…")` — title-fragment lookup, indexed, cheap |
| Descriptive content only ("the standup notes about the deploy regression") | `memex_note_search(query="…")` — ranked chunk search, expensive but right when title is unknown |

`memex_note_search` runs the full retrieval pipeline (embeddings, BM25, RRF, MMR). Reach for it only when title-fragment lookup is unavailable.

**Step 2 — Decide single-note vs cross-note coverage.**

The user's claim usually maps to *the topic*, not *a single note's mention of it*. The "Telegram media bug" likely appears in:
- The reflection note where it was first identified
- A debugging-session note from later that day
- A standup note where it was reassigned
- A "fixes shipped" note

Single-note scope misses the rest. The agent picks based on whether the topic has a stable entity anchor:

**Option A — Entity-based traversal (highest recall when topic ↔ entity).**

Check first: `memex_list_entities(query="telegram")` to see whether a usable entity exists. If yes:
```
memex_get_entity_mentions(entity_id=<id>, vault_id=…)
```
returns every unit mentioning the entity, across every note. Structural, no semantic-rank miss.

**Option B — Cross-note `memex_memory_search` with a temporal window (when no entity anchor).**
```
memex_memory_search(
    query="telegram media handler bug",
    after="2026-04-22",
    top_k=30                           # broaden — top_k=5 will miss
)
```
Drops the `note_id` scope; semantic + reranker + MMR diversity surface cross-note matches. `after`/`before` cuts noise.

**Option C — PageIndex traversal (when truly scoped to one note).**

When the agent is confident the issue lives in a single note (e.g., the user named a specific reflection):
1. `memex_get_page_indices(note_id)` → chunk layout with summaries (e.g., chunk 1 = "Standup", chunk 2 = "GitHub MCP regression", chunk 3 = "Telegram media", chunk 4 = "Other notes")
2. Agent picks the fix-relevant chunks by reading the chunk summaries
3. `memex_get_memory_units(chunk_ids=[2, 3])` (or equivalent traversal) → **all** units in those chunks, no top-k cutoff

PageIndex traversal beats `memex_memory_search(note_id=…, top_k=5)` for completeness when chunk boundaries are clean. It captures every unit in the relevant chunk; semantic top-k can miss paraphrased mentions.

**Step 3 — LLM judgment over the candidate set (mandatory).**

Whichever path returned the candidate units, the agent **must** read the unit bodies and judge which ones actually correspond to the user's claim. Memory units are short by design (single fact / observation / event, ~1–3 sentences) — the search response IS the content; reading them does not blow up context. The judgment cannot be skipped: a daily-reflection note contains episodic observations ("worked on memex 3h today") that look superficially relevant but are not fix-targets.

**Step 4 — Record outcome.**

For each fix-relevant unit:
```
memex_record_outcome(
    unit_ids=[…],
    success=false,
    reason="user says fixed; recall does not lead to useful action"
)
```
**F1's MW counters** track the failure-to-be-currently-useful. This is a **gradient** signal that compounds over future retrievals — even if a unit slips past Step 5, a future re-surface + another `success=false` will push it further down.

**Step 5 — Deprioritize.**

For the same units:
```
memory_deprioritize(unit_id, reason="user confirmed fixed 2026-04-30")
```
**F4 sets `is_deprioritized=true`** with the reason recorded. This is a **binary state** that excludes the unit from default-scope retrieval immediately; reversible via `memory_restore`.

**Why two tools, not one combined call.** `record_outcome` and `deprioritize` are orthogonal axes:

| Tool | Question it answers | Cardinality | Reversible? | Who calls it |
|------|---------------------|-------------|-------------|--------------|
| `memex_record_outcome(success=…)` | *"Did this memory help when retrieved?"* | Append-only counter (compounds across retrievals) | No (audit log) | Agents + telemetry/feedback |
| `memory_deprioritize(reason)` | *"Should this surface by default at all?"* | Binary state on the unit | Yes (`memory_restore`) | Agents + user only |

You can want one without the other:
- *Outcome=false but no deprioritize*: "this was retrieved and unhelpful — let MW compound, but maybe someone else's query still legitimately wants it." Pure gradient signal.
- *Deprioritize but no outcome*: "this was correct when written but is no longer relevant — not a failure, just stale." Verdict without judgment of past usefulness.
- *Both* (the case here): user-confirmed-fix is both a negative-usefulness signal AND a verdict that it shouldn't surface again.

A combined `memex_resolve(unit_ids, reason)` would hide the two axes; keep the primitives orthogonal and let an agent skill compose them. The rule **"for user-confirmed-fix: record_outcome=false AND deprioritize"** belongs in agent guidance (see §7.5), not as a new endpoint.

**Tomorrow's briefing — §3.4 composition does the work:**
- Default-scope retrieval excludes `is_deprioritized=true` → resolved issues drop out
- F1 MW soft-factor at the reranker further downweights anything still slipping through
- F33 exploration floor occasionally re-surfaces a low-MW unit (lets the user re-confirm it's actually fixed; if so, another `success=false` outcome compounds the downweight)
- Composed result: clean slate. No GitHub MCP regression in tomorrow's themes.

**Imperfect recall is by design.** None of Options A/B/C give *provable* 100% recall on cross-note resolution. Semantic search can miss paraphrased mentions; entity traversal misses oblique references; chunk-scoped reads miss issues split across chunks. The system tolerates this because **F33 exploration floor is the safety net**: any unit that slipped through deprioritize will occasionally re-surface, the user can confirm "still fixed," and another `record_outcome(success=false)` compounds the MW penalty. User-driven resolution is treated as a *gradient* across many turns, not a one-shot delete.

**What this validates about the existing plan:**
- F1 + F4 + KinthAI principle (§3.1) + §3.4 composition handle this everyday workflow with **no new features needed**.
- The free-text `reason` field on `memory_deprioritize` is sufficient to capture intent ("user confirmed fixed", "superseded by v2.3", "was wrong"). No enum required.
- Bulk operations (e.g., "all units from this entity / chunk / topic") are agent loops over `memex_get_entity_mentions`, `memex_memory_search`, or `memex_get_memory_units(chunk_ids=…)` + iteration — not feature gaps.
- The five-step flow above is reachable with the primitives that already ship; the gap is **agent guidance** (Step 1 disambiguation, Option A/B/C selection, Step 3 mandatory judgment, Step 4+5 paired writes), not memory-core.

**What's genuinely adjacent work (not memory-core) — agent-surface parity is non-negotiable.**

The five-step flow must be codified everywhere an agent reads tool guidance. Per the project's agent-surface parity rule, that means **all three** integration points in the same change:

1. **MCP tool descriptions** (`packages/mcp/src/memex_mcp/server.py`) — primary surface. Any MCP client (Claude Desktop, Cursor, custom clients, Hermes, Claude Code) reads these. The disambiguation rule, cross-note coverage hint, mandatory LLM judgment, and "record_outcome AND deprioritize" pairing belong in the description text of `memex_record_outcome` and `memory_deprioritize`.
2. **Claude Code plugin** (`packages/claude-code-plugin/rules/`, `skills/`, `hooks/`) — rule files and skills the Claude Code agent reads at session start.
3. **Hermes plugin** (`packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py`, `templates.py`, `tools.py`) — session briefing and template scaffolding for Hermes turns.

A change that updates only the MCP layer and skips the other two is incomplete. The Telegram cron-response handler (translating chat replies into the right MCP calls) lives on top of this codified guidance, in Hermes/Telegram integration code; §7.5 flags the integration scope.

**What is *not* a gap (resisted scope creep):**
- A `resolved_at` timestamp column. The maintenance ledger already records when deprioritize fired; the reflection note's `created_at` already anchors the original observation. Episodic recall ("what was happening last week") is the note-search path (§3.6) and reads existing timestamps.
- A `resolution_type` enum on deprioritize. Free text in `reason` carries the same information without committing the schema to a closed taxonomy.
- A `bulk-by-source` parameter on deprioritize. Agents iterate; if the loop becomes annoying in practice, optimize then.
- A combined `memex_resolve(unit_ids, reason)` endpoint. Hides the orthogonal axes (gradient outcome vs binary surface state). Compose at the agent-skill layer instead.
- Note-level deprioritize / note-rank degradation by deprioritized-fraction. Notes are episodic anchors (§3.6); deprioritizing them confuses two retrieval modes that should remain distinct.

### 3.6 Memory search vs note search: primary retrieval vs source drill-down

Memex exposes two retrieval surfaces that serve different purposes — but the split is by **granularity of inspection**, not by cognitive type.

| Tool | Purpose | Granularity | Status awareness |
|---|---|---|---|
| `memex_memory_search` | **Primary retrieval** — semantic, temporal ("April work"), entity-scoped, etc. The workhorse. | MemoryUnit (extracted facts) | Respects `is_deprioritized` natively via §3.4 composition |
| `memex_note_search` (chunks) | **Source drill-down / validation** — *"let me see what I actually wrote"* | Chunk (passages of original notes) | Returns chunks **with linked-unit status as traversal metadata** — see below |

**Memory search handles most queries**, including time-based ones. Memex's retrieval already has temporal strategies — *"what did we work on in April"* is a memory_search query with a date scope, not a note_search query. Note search is the right tool when you specifically want to read the original source: validating a fact, reviewing a session log, drilling into a reflection.

**API symmetry on temporal parameters:** both `memex_memory_search` and `memex_note_search` accept `after` and `before` ISO 8601 date params today. `memex_memory_search` also accepts `reference_date` for resolving relative phrases ("last week" → computed against the reference instead of `now()`). **`memex_note_search` should accept `reference_date` for symmetry** — otherwise the agent has to compute relative dates itself when reaching for note_search but not when reaching for memory_search. Small inconsistency; small fix. Folded into F1a's API symmetry track (~1 day of work).

**Topic-less time-window synthesis** (e.g., *"what was happening two weeks ago"* with no specific topic) doesn't fit either search well — semantic ranking has nothing to anchor on. The right shape is **`memex_survey` extended with `after`/`before`/`reference_date` params** (~1 week of work; survey already exists, just add scoping). This becomes the one-call answer for "synthesize what was happening in time period X." See §4 catalog: Tier B item.

**Note search exposes unit-status traversal in the response shape.** This extends the existing memory_links pattern (which already surfaces "this unit is contradicted by [unit_id]") to include lifecycle metadata. When a chunk is returned, the agent sees:

```
{
  chunk_id: ..., note_id: ..., note_title: "Daily Reflection — 2026-04-29",
  text: "GitHub MCP Regression: Multiple reports suggest...",
  linked_units: [
    { unit_id: ..., status: "active", is_deprioritized: true,
      deprioritization_reason: "user confirmed fixed 2026-04-30",
      links: [...]  # contradicts/weakens — already exists }
  ]
}
```

The agent reads the chunk text *plus* the meta-status of every fact derived from it. Interpretation: *"the original observation was real, but the linked units are marked resolved — don't report this as current state."*

**Why this is the right shape:**

- The relations all exist (`Note → Chunk → MemoryUnit` via `chunk_id`; memory_links already in retrieval results).
- It mirrors the existing pattern Memex uses for contradiction/supersession links.
- The historical record stays intact — note text is never rewritten or filtered by deprioritization. Agent gets full context plus interpretive metadata.
- One small extension to F1b's note_search response shape: hydrate the linked-unit status into the response. ~2-3 days, not a new catalog feature.

**Failure mode this prevents (§3.5 revisited):**

- Tomorrow's reflection cron uses `memex_memory_search` for "current themes" → resolved units drop out via §3.4 composition. Works as designed.
- If a query path causes `note_search` to surface the old reflection (validation, drill-down), chunks return with `linked_units.is_deprioritized: true` metadata → agent reads this and interprets the chunk text as historical, not current. No revisionism, no false-positive re-surfacing.

**Implications:**

1. **§3.4 composition governs memory_search.** Note_search exposes unit status as traversal metadata; the agent does the interpretation, not the ranker.
2. **Deprioritization applies to units, not notes or chunks.** Notes/chunks are immutable source records. Lifecycle is on the derived facts.
3. **F37 (originally "Episode-recall TEMPR strategy")** is redundant. Memory_search already handles temporal queries. Note_search handles drill-down. There's no third "episodic" mode that needs a new strategy. **Drop F37.**
4. **Agent prompt guidance is the lever for tool selection.** Tool descriptions need to make this concrete: memory_search for facts (including time-bounded), note_search for validation / source-text drill-down with status metadata.

---

## 4. Feature catalog

~30 features in four tiers (F1 splits into F1a/F1b/F1c sub-PRs in v6.8 — counted as one feature in summaries, three deliverables in the roadmap). Tier S and Tier A get the full six-dimension treatment. Tier B gets a brief specification. Tier C is listed with rejection reason only.

---

### TIER S — Foundation features

These four unlock everything downstream. Build them first. (F23 originally proposed here as a fifth — moved to Tier C in v6.7. Personal-scale Memex doesn't justify a multi-judge eval harness; user + reflection cron + spot-checks are sufficient signal.)

#### F1. Memory Worth 2-counter foundation (sequenced as F1a → F1b → F1c)

**Why split (added v6.8):** v6.7 packaged this as one 3-4 week feature. Apr 2026 review (point 3) flagged that schema migration, SQL parameterization, and reranker composition each have different blast radii and should land as independent PRs. F1a delivers data + write API with no retrieval changes (default behavior identical). F1b parameterizes the retrieval WHERE clauses with no-op-default flags (still no behavior change). F1c attaches the MW soft-factor at the reranker — the smallest code change but the one that actually flips behavior. Stack lands top-down; each PR is independently shippable + revertible.

**Shared source citation (applies to F1a/F1b/F1c):**
- Memory Worth (arXiv:2604.12007v1) §3.4 *Two Counts Are Necessary*: *"A single scalar MW_T(m) conceals important information that the dual-count representation preserves... A global ratio r_m near 0.5 does not by itself identify a context-dependent memory — it identifies a memory with mixed outcomes."*
- §6 *Toward Memory Governance Systems*: *"Memory Worth is designed as a foundation layer, not a complete system. Adding two counters per memory unit to architectures that already log retrievals and episode outcomes gives those systems a convergent signal."*
- D-MEM (arXiv:2603.14597v1) §4.1 informs the soft-factor multiplicative composition.

##### F1a. Schema + outcome API + DTO + API symmetry (1.5-2 weeks)

**2. New code location:**
- `packages/core/src/memex_core/services/outcomes.py` — NEW module, MW score computation (Beta-Bernoulli posterior mean) + outcome recording write path
- `packages/core/alembic/versions/XXX_add_mw_counters.py` — migration adding columns + cold-start backfill (per §6 open decision #13)
- `packages/mcp/src/memex_mcp/server.py` — new `memex_record_outcome` tool (~line 3054+)

**3. Code adapted:**
- `packages/core/src/memex_core/memory/sql_models.py`: add `success_co_count`, `failure_co_count`, `is_deprioritized` to `MemoryUnit` (~:478); add counters to `UnitEntity` (:853) and `MentalModel` (:118). NOT to global `Entity` (:701) — see Appendix B C1. **Also remove `MemoryUnit.access_count` (:554) and its index (:634)** — verified dead column (Mn8 + `WAVE-0-PREWORK.md` §5.3, plus a follow-up grep in v6.9: zero write-sites beyond the initializer, zero read-sites anywhere in `packages/`). MW provides the richer signal.
- `packages/core/src/memex_core/memory/extraction/storage.py:73` — drop the `access_count=0` initializer alongside the column removal.
- `packages/common/src/memex_common/schemas.py:354` (`MemoryUnitDTO`) — mirror new fields; the DTO already excludes `access_count` (`tests/unit/test_api_lineage_downstream.py:135` asserts this), so no DTO drop needed — only the SQL model change.
- **Stale-reference cleanup (out of scope for the migration but worth noting):** `.dev-team-artifacts/memex-poc-planning/reports/final-report.html` references `access_count` as a historical artifact. Not a live consumer; flag for archival update post-F1a.
- `packages/core/src/memex_core/services/entities.py:66` — UNCHANGED (preserve existing hybrid score; addresses Appendix B M2 by deferring the merge question rather than forcing it).
- `packages/core/src/memex_core/metrics.py` — Prometheus counters for outcome calls + MW score distribution histograms (closes Mn5).
- `packages/mcp/src/memex_mcp/server.py` — **API symmetry track**: add `reference_date` to `memex_note_search` (memory_search already has it); add `after`/`before`/`reference_date` to `memex_survey` so survey can synthesize a vault state for a time window (see §3.6).

**4. Impact:**
- **Effort:** 1.5-2 weeks. No retrieval-layer changes; default retrieval behavior is byte-for-byte identical (the new columns exist but no SQL site reads them yet).
- **Architectural choices:** vault-scoped placement (multi-tenancy preserved); Beta-Bernoulli posterior mean (`α=β=1` uniform prior per §6 #13 → score 0.5 cold-start gives `mw_boost = 1.0`); soft-factor *gated off* at this stage (no reranker change); explicit feedback API (no implicit session attribution — addresses Appendix B C4).
- **Cascades:** F4 depends on `is_deprioritized` column landing here. F33 depends on the MW columns being readable. F1b/F1c stack on top.
- **Test impact (concrete):** ~108 test files touch `MemoryUnit` (out of 259 in `packages/core/tests`); ~17 touch `ContentStatus`/`STALE`/`ACTIVE`. F1a adds nullable columns + new write path AND removes `access_count` → most existing tests should pass unchanged; the `access_count` removal needs a sweep for any test that referenced it (grep first). New tests in `tests/unit/services/test_outcomes.py`.

**5. Surface impact:**
- **CLI:** `memex memory show <unit_id>` displays MW score + 95% credible interval. New `memex memory outcome <unit_id> --success/--failure`.
- **MCP:** new `memex_record_outcome`; `memex_note_search` gains `reference_date`; `memex_survey` gains `after`/`before`/`reference_date`.
- **Hermes plugin:** Hermes briefings should recommend calling `memex_record_outcome` after using retrieved memories. No retrieval-side changes.

**6. Agent prompt text** (for `memex_record_outcome`):
```
memex_record_outcome — Record whether previously retrieved memory units contributed
to a successful outcome. Call this after you have actually used retrieved memories
to perform a task or answer a question.

- unit_ids: list of UUIDs you actually used (not all retrieved units — only the
  ones that were load-bearing in your reasoning)
- success: true if the task succeeded using these memories, false if they were
  misleading or wrong
- vault_id: the vault these units belong to
- outcome_confidence (optional, 0-1): if uncertain about the outcome, weight the
  signal accordingly. Default 1.0.

This trains Memex's retrieval ranking. The signal becomes active once F1c ships;
calls made before then are still recorded (counters increment) and will retroactively
influence ranking once F1c attaches the soft-factor.

Call generously. Silence provides no learning signal.
```

##### F1b. Retrieval scope parameterization at the centralized filter (1-1.5 weeks)

Resolves Appendix B C3 — turns the retrieval-layer's hardcoded `WHERE status = ACTIVE` filter into the parameterized scope matrix from §3.1. Wave 0 verification (`WAVE-0-PREWORK.md` §3) showed the scope is **smaller than v6.8 implied**: 8 strategy sites all flow through one centralization point (`apply_generic_filters`), and `document_search.py` should NOT receive the filter (it operates on `Chunk`/`Node`, not `MemoryUnit` — see §3.6 for why note search shows status as response metadata instead).

**2. New code location:** None — parameterizes existing strategies.

**3. Code adapted (refined v6.9 against current source):**
- `packages/core/src/memex_core/memory/retrieval/strategies.py:78-92` (`apply_generic_filters`) — **the centralized injection point**. Add one branch: `if not include_deprioritized: statement = statement.where(col(MemoryUnit.is_deprioritized) == False)`. This single change covers all 8 strategy sites that already flow through this function (lines 90, 238, 721, 763, 957, 980, 1004, 1210).
- `packages/core/src/memex_core/memory/retrieval/engine.py:233` (`RetrievalRequest`) — add `include_deprioritized: bool = false` and pass through to strategies via existing kwargs plumbing.
- `packages/mcp/src/memex_mcp/server.py` — surface `include_deprioritized` on `memex_memory_search` (the primary affected MCP tool — `memex_note_search` and `memex_recent_notes` are note-/chunk-level and don't apply this filter; they expose status as traversal metadata per §3.6 instead).
- `packages/common/src/memex_common/schemas.py` — request DTO mirror.
- **Explicitly NOT touched:** `document_search.py` (Chunk/Node level, not MemoryUnit — show deprioritized content with status metadata, do not filter); `engine.py:1357` (virtual unit construction from MentalModels — virtual units never carry `is_deprioritized`); `Note.status='archived'` filters (handled by F4/archive separately, see §6 #12).

**Total scope:** **1 function change + 1 request model + 1 MCP tool + 1 DTO = 4 surfaces** — much narrower than v6.8's "11 sites + 2 entry points" framing implied. The 8 strategy queries inherit the change automatically because they all already pipe through `apply_generic_filters` (the existing `include_stale` kwarg proves the pattern works).

**4. Impact:**
- **Effort:** 1-1.5 weeks. Most cost is in test design, not code; the centralization saves on per-site review burden.
- **Architectural choices:** scope flag defaults `false` → no behavior change unless caller opts in. The scope filter happens **before step 4** of the §3.4 pipeline (it's a SQL WHERE, not a reranker factor). Centralizing in `apply_generic_filters` matches the existing `include_stale` pattern.
- **Cascades:** F4 becomes safely callable (deprioritized units stay retrievable via `include_deprioritized=true`). F1c can stack independently.
- **Test impact (concrete):** ~117 test files touch `retrieval`/`strategies` but most don't exercise `apply_generic_filters`-level filtering directly. Default-scope tests pass unchanged; new tests in `tests/integration/test_int_retrieval_scope.py` (NEW) cover both states of `include_deprioritized` (true/false) plus interaction with `include_stale`.

**5. Surface impact:**
- **MCP:** `memex_memory_search` gets `include_deprioritized` kwarg. (`memex_note_search` / `memex_recent_notes` unchanged at the SQL filter level — they surface status as response metadata per §3.6.)
- **Hermes plugin:** `recall` gets the scope param.

**6. Agent prompt text** (append to `memex_memory_search` description):
```
Retrieval scope (set via include_deprioritized):
- default (false): active, non-deprioritized memories. Use for most queries.
- explicit_recall (true): also includes deprioritized memories.
  Use for "remember when..." queries, direct references to past discussions, or
  when the user explicitly asks about something you suspect was deprioritized.

For source drill-down on archived/deprioritized notes, use memex_note_search —
note search returns chunks with linked-unit status as traversal metadata so you
can see lifecycle without losing the historical record.
```

##### F1c. MW soft-factor composition at the reranker (~1.5 weeks)

The smallest code change in the F1 stack — flips MW from "data being collected" to "signal influencing rank." Uses the **additive-marginal formula** from §3.4 (refined v6.9 per `WAVE-0-PREWORK.md` §4.2 — the pure-sigmoid form was a cold-start trap).

**2. New code location:** None — extends the reranker site identified in §3.4.

**3. Code adapted:**
- `packages/core/src/memex_core/memory/retrieval/engine.py:1140` — extend the chain from `ce_score × recency_boost × temporal_boost` to `... × mw_boost`, where `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)` and `mw_score = (success_co_count + 1) / (success_co_count + failure_co_count + 2)` (Beta-Bernoulli posterior mean with α=β=1 uniform prior).
- `packages/core/src/memex_core/services/outcomes.py` (NEW from F1a) — expose `compute_mw_boost(unit, mw_alpha)` for the reranker to call
- `packages/core/src/memex_core/memory/retrieval/config.py` — new config knob `mw_alpha` (default 0.3, matching the existing `recency_alpha` and `temporal_alpha` magnitudes)
- `packages/core/src/memex_core/metrics.py` — histogram `memex_mw_boost` so the cron + spot-check can observe boost distribution + drift
- Hydration step: batch-fetch `success_co_count`, `failure_co_count` columns alongside existing unit fields (no extra DB roundtrip)

**4. Impact:**
- **Effort:** ~1.5 weeks (was ~1w in v6.8; +0.5w for benchmark on a test corpus per `WAVE-0-PREWORK.md` §6).
- **Architectural choices:** additive-marginal composition (no cold-start zeroing); strictly upstream of MMR per §3.4; α=β=1 prior + no warm-up gate (§6 #13); single `mw_alpha` knob, no per-vault override in v1.
- **Cascades:** F11 (FSFM decay, Tier B) plugs into the same composition site when it ships. F33 exploration floor (which injects low-MW units) becomes meaningful — without F1c, the injection has no rank consequence.
- **Test impact (concrete):** ~74 test files touch `rerank`/`reranker`. Most are unaffected (boost is multiplicative on top of existing factors); new tests in `tests/integration/test_int_retrieval_mw_composition.py` (NEW) cover (a) cold-start units (MW undefined) get `mw_boost = 1.0` (no rank change), (b) high-MW units rank higher than low-MW peers with same ce_score, (c) MMR still applies after composition.

**5. Surface impact:** None new — invisible to MCP/CLI surface. Quality signal is observed via reflection cron + spot-check (§3.3) plus a one-time before/after benchmark on a test corpus (per `WAVE-0-PREWORK.md` §6).

**6. Agent prompt text:** None — composition is a back-end ranker change, not an API surface.

---

#### F2. D-MEM Z-score embedding anisotropy correction

**1. Source citation:**
- D-MEM (arXiv:2603.14597v1) §4.1 *Agentic RPE — Semantic Surprise*: *"Modern high-dimensional embedding models often suffer from representation anisotropy, where vectors cluster tightly in a narrow cone, causing even semantically unrelated texts to yield cosine similarities above 0.7. To prevent the surprise metric from being compressed into a non-discriminative range, we maintain a sliding window of the last k similarity scores to calculate a historical mean μ_sim and standard deviation σ_sim. We then apply a Z-score normalization mapped through a sigmoid function."*

**2. New code location:**
- `packages/core/src/memex_core/memory/models/anisotropy.py` — NEW module, sliding-window normalizer

**3. Code adapted:**
- `packages/core/src/memex_core/memory/retrieval/engine.py` — wrap cosine similarity calls with the normalizer
- `packages/core/src/memex_core/memory/contradiction/engine.py` — same for contradiction-detection similarity calls
- `packages/core/src/memex_core/memory/extraction/` — same for entity-resolution similarity calls (deduplication)

**4. Impact:**
- **Effort:** 2-3 days (Very Low)
- **Architectural choices:** sliding-window size (e.g., k=1024), epsilon for numerical stability. No schema changes.
- **Cascades:** none. Pure pre-processing improvement on top of existing ONNX FastEmbedder. Affects all downstream similarity-based decisions.

**5. Surface impact:**
- **CLI:** none directly; better retrieval quality across all CLI search operations.
- **MCP:** none directly; better quality on `memex_memory_search` and related.
- **Hermes:** none directly; better quality on `recall`, `retrieve_notes`, `survey`, entity tools.

**6. Agent prompt text:**
None needed — invisible to the agent. Improvement is in retrieval quality, not in the API surface.

---

#### F23. ~~Multi-judge retrieval evaluation harness~~ → moved to Tier C (v6.7)

Reason for demotion: enterprise-scale formalism with no proportionate payoff at personal scale. The user is the judge; daily reflection cron + lived experience provide ongoing signal. Lightweight alternative if needed: a pinned 20-30 query regression set, manually verified before/after major changes (~1 week, not 5-7). See Tier C entry. Reconsider F23 only if Memex moves toward multi-tenant deployment or research-claim publication.

---

#### F25. Write-time importance + intent + risk classifier

**1. Source citation:**
- FSFM (arXiv:2604.20300v2) §3.2 *Implementation Architecture*: *"Modular system architecture consisting of four components: UltraSafeMemoryManager, ImportanceScoringEngine, SelectiveForgettingMechanism, PerformanceBenchmarkingTool."*
- D-MEM (arXiv:2603.14597v1) §4.1 *Long-term Utility*: *"The Critic Router performs a fundamental lifecycle classification... Inputs are categorized into three temporal tiers: Transient (zero-information phatic fillers or momentary states), Short-Term (days-to-weeks relevance), or Persistent (months to permanent traits)."*
- @kinthaiofficial comment on #64 (production analog: Permanent / Durable / Ephemeral).

**2. New code location:**
- `packages/core/src/memex_core/memory/extraction/classifier.py` — NEW module, constrained-JSON LLM classifier
- `packages/core/alembic/versions/XXX_add_intent_risk_columns.py` — migration

**3. Code adapted:**
- `packages/core/src/memex_core/memory/sql_models.py` — add `intent_class: str` enum (`permanent`/`durable`/`ephemeral`) and `risk_class: str` enum (`none`/`sensitive`/`private`/`safety`) to `MemoryUnit`
- `packages/core/src/memex_core/memory/extraction/` — call the classifier as a pipeline stage after fact extraction, before persistence
- `packages/common/src/memex_common/schemas.py:354` — DTO mirror
- `packages/core/src/memex_core/llm.py` — extend executor for constrained-JSON classifier calls (already supports DSPy structured output)

**4. Impact:**
- **Effort:** 2-3 weeks (Moderate)
- **Architectural choices:** classifier model (cheap default, e.g., Haiku 4.5); constrained-JSON schema; default intent (`durable`) for unclear cases; risk-class action policy (sensitive → flagged for linter; private → excluded from default retrieval; safety → blocked entirely).
- **Cascades:** F11 (decay) becomes principled (intent-class drives decay rate); F26 (risk classification) is essentially this feature applied to the risk dimension; F30 (distillation) becomes possible once enough labels exist.

**5. Surface impact:**
- **CLI:** `memex memory show <id>` displays intent + risk; `memex memory list --intent ephemeral` filter
- **MCP:** existing tools that return MemoryUnit DTOs include the new fields; new optional `intent_override` param on `memex_save_note` for explicit user control
- **Hermes:** `retain` tool gains optional `intent` and `risk` parameters; agent can declare lifecycle at write time

**6. Agent prompt text:**

For `retain` and similar write-side tools:
```
When saving content to memory, optionally specify:

- intent: lifecycle class
  * "permanent" — identity, preferences, key facts that should never decay
    (e.g., "user has peanut allergy", "user prefers ruff over black")
  * "durable" — project decisions, relationship state, multi-week relevance
    (default if unspecified)
  * "ephemeral" — task context, session details, days-to-weeks relevance only

- risk: content sensitivity
  * "none" — default, public-safe content
  * "sensitive" — flagged for linter review
  * "private" — excluded from default retrieval; surfaced only on explicit query
  * "safety" — blocked entirely (Memex will refuse to ingest)

Set intent based on the content's actual durability, not your estimate of importance.
A specific date for an event next week is "ephemeral" even if the event is important.
The user's home address is "permanent" even though it's not exciting.
```

---

#### F33. MW exploration floor

**1. Source citation:**
- Memory Worth (arXiv:2604.12007v1) §5.3 *Assumption (A3) Violation Studies*: *"Experiment 3 demonstrates that a self-correcting feedback loop effectively stabilizes memory weights and prevents degenerate concentration in stationary environments."* The paper identifies "rich-get-richer" bias: memories with high MW are retrieved more, accumulate more positive signals, and dominate retrieval — while low-MW memories never get a chance to demonstrate value.

**2. New code location:**
- `packages/core/src/memex_core/memory/retrieval/exploration.py` — NEW module, ε-injection logic

**3. Code adapted:**
- `packages/core/src/memex_core/memory/retrieval/engine.py` — after MMR, inject 1 random low-MW unit per N results with probability ε (default 0.05)
- `packages/core/src/memex_core/services/outcomes.py` (from F1) — exploration-injected units flagged as such; their outcome calls are tracked separately

**4. Impact:**
- **Effort:** 1 week (Low)
- **Architectural choices:** exploration probability ε; how to select exploration candidates (uniform random over low-MW set, or stratified by entity); flagging mechanism so exploration outcomes don't conflate with regular retrieval signals.
- **Cascades:** complements F1 — without F33, MW counters can never improve for never-retrieved memories.

**5. Surface impact:**
- **CLI:** none directly
- **MCP:** retrieval results may include 1-2 unexpected items; tool documentation should explain that exploration units carry an `exploration: true` field
- **Hermes:** same — Hermes recall results occasionally include exploratory units

**6. Agent prompt text:**

Append to retrieval tool descriptions:
```
A small fraction of returned results may be exploration injections — memories with
low confidence scores that are surfaced periodically to gather signal. These will
have `exploration: true` in their metadata. If an exploration unit is useful, call
memex_record_outcome with success=true to lift its rank. If it's not relevant,
record success=false. This is how Memex avoids rich-get-richer dynamics.
```

---

### TIER A — High-value additions

#### F4. `memory_deprioritize` MCP tool

**1. Source:** KinthAI comment on #64 — *"forgetting is not about deleting data — it is about adjusting retrieval weights."* Issue #53 introduces the broader memory-control tool family. The non-destructive principle (§3.1) makes this safe.

**2. New code location:** `packages/mcp/src/memex_mcp/server.py` — new tool. Uses F1a's `is_deprioritized` column.

**3. Code adapted:**
- `packages/core/src/memex_core/services/units.py` (or `services/notes.py`) — new `set_unit_deprioritized()` method (does NOT cascade to MentalModels; does NOT call `prune_stale_evidence`)
- F1a column `is_deprioritized` is the underlying mechanism; F1b makes default-scope queries skip these units; F4 only requires F1a (can ship before F1b/F1c).

**4. Impact:**
- **Effort:** 1 week (Low — schema work counted in F1a)
- **Architectural:** orthogonal to status enum; reversible by flipping flag; no cascading effects.
- **Cascades:** none if isolated; combines with F8 + F32 for full curation workflow.

**5. Surface impact:**
- **CLI:** `memex memory deprioritize <id>` and `memex memory restore <id>` commands
- **MCP:** new `memory_deprioritize(unit_id, reason)` tool
- **Hermes:** new tool exposed in plugin; Hermes can curate its own sessions' noise

**6. Agent prompt text:**
```
memory_deprioritize — Lower a memory unit's retrieval rank without deleting it.
Use when a memory is misleading, outdated, or noise that contaminates retrieval.

- unit_id: the unit to deprioritize
- reason: brief text explanation (logged to maintenance ledger). Use this field
  liberally to capture WHY (e.g., "user confirmed issue fixed", "superseded by
  v2.3 release", "was wrong about deploy target"). Free text is sufficient — no
  enum needed.

The unit remains accessible via include_deprioritized=true retrieval. To restore,
the user runs `memex memory restore <id>`. This is non-destructive — prefer it
over hard delete in almost all cases. Use sparingly: a small number of high-quality
deprioritizations is more valuable than aggressive pruning.
```

---

#### F5. `memory_summarize_node` MCP tool — just expose existing reflection

**1. Source:** Issue #53. ZenBrain §B.3 motivates on-demand consolidation.

**2. New code location:** `packages/mcp/src/memex_mcp/server.py` — new tool, delegates to existing `ReflectionService.reflect()` (`services/reflection.py:88`) and `POST /reflections` (`server/reflection.py:32`).

**3. Code adapted:**
- None to core (reflection path already exists)
- `packages/core/src/memex_core/services/reflection.py:55` — note the `asyncio.Lock` is process-local; multi-worker concurrent calls on the same entity are not serialized. Acceptable for v1; F9 introduces distributed locking.

**4. Impact:**
- **Effort:** 2-3 days (Very Low)
- **Architectural:** none new; pure exposure
- **Cascades:** rate-limit needed to prevent agent over-triggering.

**5. Surface impact:**
- **CLI:** `memex reflect entity <id>` already exists; nothing new.
- **MCP:** new `memory_summarize_node(entity_id | note_ids[], scope)` tool
- **Hermes:** exposed in plugin; Hermes can request consolidation when noticing conflict mid-session.

**6. Agent prompt text:**
```
memory_summarize_node — Trigger reflection synchronously on a specific entity or
note set. Use when you notice mid-conversation that retrieved facts about a topic
are conflicting, incomplete, or scattered, and you want Memex to consolidate them
into a coherent mental model before continuing.

- entity_id: focus reflection on a single entity (preferred for per-topic work)
- note_ids: alternatively, focus on a specific set of notes
- scope: "incremental" (default — only new evidence) or "full" (re-evaluate all)

Returns a ReflectionResult with the updated/new MentalModel(s). Use sparingly;
reflection is LLM-intensive. Default to background reflection unless you have a
specific in-session reason to trigger now.
```

---

#### F6. Maintenance ledger + rule-based linter (vault-scoped, single-leader)

**1. Source:** Issue #18; FSFM §2.4 (governance); ZenBrain §B.11 (audit-trail concept).

**2. New code location:**
- `packages/core/alembic/versions/XXX_maintenance_proposals.py` — migration
- `packages/core/src/memex_core/services/lint.py` — NEW module (per-vault and global modes)
- `packages/core/src/memex_core/sql_models.py` — new `MaintenanceProposal` table

**3. Code adapted:**
- `packages/core/src/memex_core/scheduler.py` — register lint as another `@clock.task` under the existing `MEMEX_LEADER_LOCK_ID` leader (no new lock)
- `packages/core/src/memex_core/metrics.py` — Prometheus counters for lint findings by type/vault

**4. Impact:**
- **Effort:** 3-4 weeks (Moderate)
- **Architectural:** read-only by design; vault_id nullable on the table (NULL = global findings); ledger pattern (audit log, not approval queue per §3.1).
- **Cascades:** F8 (MCP tool to read flags), F10 (surprise-gated LLM lint extends this), F32 (diagnostics surface lint state).

**5. Surface impact:**
- **CLI:** `memex lint status [--vault X | --global | --all]`, `memex lint findings [--type structural|quality|governance|schema]`
- **MCP:** none directly (use F8)
- **Hermes:** `survey` tool can include lint summary in session briefings

**6. Agent prompt text:** None directly (use F8).

---

#### F8. `memex_get_lint_flags` MCP tool

**1. Source:** Issue #18; closes the agent-triage loop in your Memory PR proposal.

**2. New code location:** `packages/mcp/src/memex_mcp/server.py` — new tool, queries F6's table.

**3. Code adapted:**
- `packages/core/src/memex_core/services/lint.py` (from F6) — query method with filters

**4. Impact:**
- **Effort:** 1 week (Low)
- **Architectural:** read-only; supports vault_id filter, lint_type filter, status filter.
- **Cascades:** completes F6 → F8 → F4 hygiene loop.

**5. Surface impact:**
- **CLI:** see F6
- **MCP:** new `memex_get_lint_flags(vault_id?, lint_type?, status?, limit?)` tool
- **Hermes:** Hermes session briefings can include "N pending lint findings in this vault"

**6. Agent prompt text:**
```
memex_get_lint_flags — List pending memory-hygiene findings the linter has detected.
Use periodically (e.g., once per long session) or when the user asks about memory state.

- vault_id (optional): scope to a single vault. Omit for all-vault view.
- lint_type (optional): structural | quality | governance | schema
- status (optional): pending | resolved | dismissed (default: pending)
- limit (default 20)

Each finding includes: target_id, lint_type, evidence (why detected), suggested_action.
Most findings can be auto-resolved by calling the relevant tool (e.g., memory_deprioritize
for low-MW units). Surface high-confidence findings to the user; act autonomously on
low-risk ones (deprioritize, mark stale).
```

---

#### F9. `memory_reconsolidate` + `memory_consolidate` MCP tools + per-entity advisory lock

**1. Source:** Issue #53. ZenBrain §B.7 ReconsolidationEngine (rollback logs concept).

**2. New code location:**
- `packages/core/src/memex_core/services/locks.py` — NEW module for per-entity Postgres advisory lock helper (this is **new infrastructure**, not "reuse existing")
- `packages/mcp/src/memex_mcp/server.py` — new tools

**3. Code adapted:**
- `packages/core/src/memex_core/memory/contradiction/engine.py:39-45` — `detect_contradictions(unit_ids, vault_id)` — already exists, just expose. Note: takes `unit_ids`, not entities — F9 includes an entity → unit_ids resolver step.
- `packages/core/src/memex_core/memory/reflect/reflection.py` — use existing `reflect_batch`

**4. Impact:**
- **Effort:** 3-4 weeks (Moderate-High — per-entity lock is new infrastructure)
- **Architectural:** distributed locking (Postgres advisory locks per entity_id hash); entity → unit_ids resolver pattern; consolidate writes to maintenance ledger as it goes (no queue-and-wait).
- **Cascades:** the per-entity lock is reusable infrastructure for any future per-entity write operation.

**5. Surface impact:**
- **CLI:** `memex memory reconsolidate <entity_id>`, `memex memory consolidate --vault X`
- **MCP:** two new tools
- **Hermes:** exposed; useful when Hermes notices contradictions mid-session

**6. Agent prompt text:**
```
memory_reconsolidate — Re-evaluate memories for a specific entity, detecting
contradictions and updating mental models. Use when you notice retrieved facts
about an entity disagree.

- entity_id: target entity

Runs contradiction detection across all units linked to the entity, then triggers
reflection to produce updated mental models. LLM-intensive — use only when there
is concrete evidence of conflicting information.

memory_consolidate — Vault-wide batch curation. Identifies low-MW + stale units
and deprioritizes them. Writes findings to the maintenance ledger.

- vault_id: target vault
- dry_run (default false): if true, preview without making changes

Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer
memory_reconsolidate.
```

---

#### F10. Surprise-gated LLM-assisted lint

**1. Source:** Issue #18 (your Comment 1 design); D-MEM §4.1 surprise calc.

**2. New code location:**
- `packages/core/src/memex_core/services/lint_llm.py` — NEW module for LLM-triggered checks

**3. Code adapted:**
- `packages/core/src/memex_core/memory/extraction/` — surprise calculation alongside chunking (uses F2's anisotropy normalizer)
- `packages/core/src/memex_core/services/lint.py` (F6) — register LLM checks as a separate class triggered by surprise threshold

**4. Impact:**
- **Effort:** 2-3 weeks (Moderate)
- **Architectural:** surprise threshold (initial 0.7, tunable); cost cap per vault per day; LLM check types (semantic contradiction, schema drift).
- **Cascades:** depends on F2 + F6.

**5. Surface impact:**
- **CLI:** lint findings now include `source: rule | llm` distinguisher
- **MCP:** F8 returns LLM-triggered findings same as rule-based ones
- **Hermes:** unchanged surface

**6. Agent prompt text:** Same as F8 — agent doesn't need to know whether a finding came from rule-based or LLM-triggered check.

---

#### F14. Procedural observations via KV namespace (lightweight)

**Diverges from Apr 2026 batch (`a1472a80` P1) — see §2.3.1 for rationale.** That note proposes SKILL.md filesystem artifacts inside Memex; this report argues the SKILL.md *artifact* belongs at the agent layer (where Codex/Hermes already put it) and Memex should store only the *observations* about how an agent's existing procedures should adapt to specific contexts.

**1. Source citation:**
- Memex internal: design doc §7.3 (vault note `2f21fd83`) "Experiential Memory" gap — *the gap is real; this is a lighter implementation than that note proposes*.
- Apr 2026 cross-system feature batch (vault note `a1472a80`) P1: "SKILL.md procedural memory" — **not adopted as-is**; see §2.3.1 for why.
- @kinthaiofficial on #64: *"When the agent needs to do something, procedural memory is most relevant"* — observations support the procedure; they aren't the procedure itself.

**2. New code location:**
- None. Uses existing `services/kv.py` + `KVEntry` table at `sql_models.py:1301`.

**3. Code adapted:**
- `packages/core/src/memex_core/services/kv.py` — add a small validator/convention helper so `procedure:` keys carry structured tags (`vault`, `agent`, `context_class`)
- `packages/core/src/memex_core/services/outcomes.py` (NEW from F1) — extend `record_outcome` to accept `target_type='kv_key'` so procedural observations gain MW counters
- `packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py` (or equivalent briefing builder) — query `kv_search(prefix='procedure:', context=current_task)` and inline matches in session briefings
- `packages/mcp/src/memex_mcp/server.py` — KV tools already exist (`memex_kv_get`, `memex_kv_search`, `memex_kv_write`); document the `procedure:` prefix as the procedural namespace

**4. Impact:**
- **Effort:** ~1 week (Low). KV is already in place; F1 already proposes the outcome API extension; Hermes briefing is the only substantive integration.
- **Architectural choices:**
  - **Substrate:** existing KV (global with namespace prefixes — appropriate, since procedural observations may apply cross-vault when scoped by user/agent rather than vault).
  - **Key convention:** `procedure:<verb>:<context-tag>` — e.g., `procedure:deploy:user-jasper-staging`, `procedure:commit-message:style-heredoc`, `procedure:test-runtime:repo-foo-uses-uv`.
  - **Collision policy (when two agents write the same key):** last-writer-wins on the `value` field with a `version` increment, **but** retain the previous value in a `procedure:<verb>:<context-tag>:history` JSON array (capped at last 5 entries) so superseded observations stay inspectable. The agent reads the latest value by default; `memex_kv_get(..., include_history=true)` returns the full chain. Rationale: procedural observations are claims about how the world works for this user/project; conflicting observations are evidence of drift, not noise to discard. The history lets the linter (F6) flag procedures that flip-flop and surface them for human review.
  - **Extraction:** none. Observations are written by the agent (or the user) when a useful adaptation is noticed. No Phase 1/Phase 2 pipeline. No LLM-driven pattern mining. The agent decides when to write; KV does the storage.
  - **Outcome tracking:** F1's `memex_record_outcome` accepts a `kv_key` target. Observations that consistently lead to good outcomes get higher MW; bad ones get downweighted via F33's exploration floor.
- **Cascades:** benefits from F1 (outcome counters on KV keys). No dependency on F25.

**5. Surface impact:**
- **CLI:** `memex procedure list [--prefix X]`, `memex procedure add <key> <value>`, `memex procedure show <key>` — thin wrappers over `memex kv` for discoverability
- **MCP:** existing KV tools cover this; just document the `procedure:` prefix
- **Hermes:** session briefings include "Procedural observations relevant to this task: ..."; agent reads them and adapts its own (agent-owned) skill execution accordingly

**6. Agent prompt text:**
```
Procedural observations are tips Memex has learned about how to adapt YOUR existing
skills to specific contexts (this user, this project, this codebase). They are NOT
replacements for your built-in skills — they are observations to consider before
or during execution.

Examples:
- procedure:deploy:user-jasper      → "uses staging by default; user has been
                                       burned by late-night prod deploys"
- procedure:commit-message:style    → "user prefers HEREDOC, not -m"
- procedure:test-runtime:repo-foo   → "this codebase uses uv, never pip"

memex_kv_search(prefix='procedure:', context=...) — find procedural observations
relevant to your current task. Briefings surface high-relevance ones automatically;
call directly when you're about to do something significant and want to check.

memex_kv_write(key='procedure:...', value=...) — record a new observation when you
notice a useful adaptation. Be specific about the context. Don't write procedures
themselves (those live in your own skills system); write what's special about
applying them here.

memex_record_outcome(target_type='kv_key', kv_key='procedure:...', success=...) —
record whether following this observation led to a successful outcome. Trains MW
on procedural observations, same as on memory units.
```

**Disagreement with Apr 2026 batch:** that note's P1 SKILL.md inside Memex would duplicate the agent layer's skills system. Recommendation: revise that batch entry. The lighter KV-based observations approach delivers the cross-agent value (one user-context adaptation, multiple agents) without the substrate duplication.

---

#### F20. FSRS-based memory revisitation

**1. Source citation:**
- **Foundational:** Ebbinghaus (1885) — exponential forgetting curve `R = exp(-t/S)` where R is retention, t is time elapsed, S is stability. Pimsleur (1967) and SuperMemo (Wozniak 1990) extended this into spaced-repetition scheduling. FSRS is the modern open-source descendant.
- ZenBrain (arXiv:2604.23878v1) §B.2 *vmPFC-Coupled FSRS with Prediction-Error Signals*: *"Computing cosine distance between context embeddings to generate signals adjusting repetition intervals via sigmoid re-encoding factor."*
- FSFM §5.5 references spaced repetition for educational AI.
- ZenBrain Appx (vault note `5992f782`) confirms its retention model is "Ebbinghaus + Two-Factor + vmPFC-FSRS + Sim-Select" — F20 adopts the FSRS layer; F11 covers the basic decay layer; the other two layers are deliberately out of scope.

**2. New code location:**
- `packages/core/src/memex_core/memory/revisit.py` — NEW module, FSRS scheduler
- `packages/core/alembic/versions/XXX_revisit_columns.py` — migration

**3. Code adapted:**
- `packages/core/src/memex_core/memory/sql_models.py` — add `next_review_at`, `review_interval_days`, `review_stability` to `MemoryUnit`
- `packages/core/src/memex_core/scheduler.py` — register a daily revisit task that selects units due for review and surfaces them via the maintenance ledger or to Hermes briefings
- `packages/core/src/memex_core/services/outcomes.py` (F1) — successful retrieval extends review interval; failed retrieval shortens it

**4. Impact:**
- **Effort:** 3-4 weeks (Moderate)
- **Architectural:** which units get FSRS scheduling (default: high-importance + intent=permanent/durable); how surfaces appear (briefings, lint findings, or new dedicated MCP tool); interaction with MW (high-MW units may not need revisitation prompts).
- **Cascades:** depends on F1 (MW signal) and F25 (intent class). Complements F11 (decay) — units due for review can have decay paused.

**5. Surface impact:**
- **CLI:** `memex review due [--vault X]` shows units due for review; `memex review complete <id> --quality {again|hard|good|easy}` records the review
- **MCP:** `memex_get_due_for_review(vault_id)` — returns units due
- **Hermes:** session briefings include "N memories due for review in this vault"; new `memory_review` tool

**6. Agent prompt text:**
```
memex_get_due_for_review — Returns memory units that the FSRS scheduler has flagged
as due for review (analogous to Anki cards). Use at session start (briefings cover
this automatically) or when the user asks about pending review.

When you encounter a due unit during normal work and it remains accurate, call
memory_review(unit_id, quality="good") to extend its review interval. If the unit
turns out wrong or outdated, call with quality="again" or "hard" to shorten the
interval and flag it for closer attention.

This trains Memex's adaptive review schedule — high-stability memories get reviewed
less often; memories that need correction get reviewed more.
```

---

#### F38. Consolidation orchestration (replaces F21)

**1. Source citation:**
- Codex pattern (vault note `d9ca8ba7`): Phase 2 consolidation as a single global job, file-locked, runs against a diff of new/changed units.
- Apr 2026 batch (vault note `a1472a80`) P3: explicit Auto-Dream prune phase with file-lock mutex and "≥24h AND ≥5 sessions" gate (Claude Code pattern `4bdf8c83`).
- Memex internal: existing `services/reflection.py` (event-driven, entity-level) + `services/contradiction/engine.py` (per-unit-batch) + `services/mental_model_cleanup.py` (reactive on delete) — no orchestration that runs them periodically as a unit.

**2. New code location:**
- `packages/core/src/memex_core/services/consolidation.py` — NEW thin orchestrator that, per vault and per scheduling tick, runs reflection + contradiction + cleanup in a coordinated batch
- `memex consolidate` CLI subcommand (in existing CLI module)

**3. Code adapted:**
- `packages/core/src/memex_core/scheduler.py` — register `consolidation_tick` as a `@clock.task` under existing `MEMEX_LEADER_LOCK_ID` (no new lock); default cadence: nightly per vault
- F1 outcome data feeds the diff selection (only consolidate units with new outcome signals since last tick)
- Optional: F11 decay step folded in once F11 ships

**4. Impact:**
- **Effort:** ~1 week (Low). 80% of the work is a new top-level orchestrator that calls existing services in sequence with shared diff state.
- **Architectural:** purely orchestration — no new memory mechanics. Diff selection (Codex pattern) bounds cost: `units_changed_since_last_tick` only. Per-vault to enable cadence overrides.
- **Cascades:** none destructive. Existing tests for reflection/contradiction/cleanup remain authoritative; orchestrator gets its own integration test for the diff-selection logic.

**5. Surface impact:**
- **CLI:** `memex consolidate [--vault X] [--dry-run]` triggers manually; `memex consolidate status` shows last-run timestamps per vault
- **MCP:** none (background)
- **Hermes:** none directly

**6. Agent prompt text:** None — internal infrastructure.

**Why this replaces F21:** F21 (full ZenBrain sleep loop) was 6–8 weeks of TAG-style LTP/LTD machinery, counterfactual replay, and edge reweighting — an attempt to port a discredited paper's biological framing into Memex. Most of what it wanted (periodic batch consolidation, usage-based pruning, edge-weight maintenance) is either already covered by existing services run together, or trivially added as scheduler entries gated by the existing leader. The genuinely novel parts of F21 (counterfactual retrieval replay) are research features with no validated payoff and require F23 to even score — defer to a future research feature, do not block the foundation roadmap on them. F21 is therefore demoted to Tier C; F38 captures the operational benefit at ~1/8 the cost.

---

#### F26. Risk classification at extraction

**1. Source citation:**
- FSFM (arXiv:2604.20300v2) §1.1 *Background and Motivation*: *"Indiscriminate memory retention creates significant attack surfaces within systems, where malicious actors can exploit improperly stored sensitive information such as user credentials, dangerous remarks, private data, and configuration details."*
- §6.1 result: *"100% elimination of dangerous content."*

**2. New code location:** Subsumed by F25 (the classifier produces both intent and risk in a single LLM call).

**3. Code adapted:**
- `packages/core/src/memex_core/memory/extraction/classifier.py` (F25) — add risk dimension to the constrained-JSON output
- `packages/core/src/memex_core/memory/retrieval/strategies.py` — add risk-class filter (private content excluded from default scope, surfaced only on explicit query)

**4. Impact:**
- **Effort:** 1 week additional on top of F25 (Low)
- **Architectural:** risk-class semantics: `none` (default), `sensitive` (linter flag), `private` (excluded from default retrieval), `safety` (refuse to ingest)
- **Cascades:** combines with F6 (linter surfaces sensitive findings) and F1 (private memories require explicit recall scope).

**5. Surface impact:**
- **CLI:** `memex memory list --risk sensitive` filter
- **MCP:** retrieval tools respect risk filtering
- **Hermes:** private memories not surfaced in default `recall`; explicit recall required

**6. Agent prompt text:** Covered by F25's prompt.

---

#### F32. Memory diagnostics (UMAP + heatmap + lint dashboard)

**1. Source citation:**
- D-MEM (arXiv:2603.14597v1) Appendix A.3 *Memory Manifold: Structural Stability Under Noise*: *"By applying UMAP dimensionality reduction to visualize memory embeddings, distinguishing between LTM (persistent nodes) and STM (buffer entries)."*
- Appendix A.2 *Attention Heatmap*: visualization of multi-hop retrieval patterns.

**2. New code location:**
- `packages/core/src/memex_core/diagnostics/` — NEW subpackage
- `packages/core/src/memex_core/diagnostics/umap.py` — UMAP projection of MemoryUnit embeddings
- `packages/core/src/memex_core/diagnostics/heatmap.py` — retrieval-frequency heatmap data
- `packages/core/src/memex_core/server/diagnostics.py` — new HTTP endpoints serving JSON for visualization
- `packages/cli/src/memex_cli/diagnose.py` — NEW command group

**3. Code adapted:**
- `packages/core/src/memex_core/server/__init__.py` — register diagnostics router
- (Optional) frontend visualization in `packages/web` if it exists; otherwise CLI emits JSON for external tools

**4. Impact:**
- **Effort:** 3-4 weeks (Moderate)
- **Architectural:** UMAP runs offline (cached); heatmap aggregates from F1 outcome data; lint dashboard aggregates F6 findings.
- **Cascades:** observability for the entire governance stack — without F32, F1/F6/F11 effects are invisible.

**5. Surface impact:**
- **CLI:** `memex diagnose manifold --vault X` (saves UMAP JSON), `memex diagnose retrieval --vault X` (heatmap), `memex diagnose lint --vault X` (dashboard)
- **MCP:** `memex_get_diagnostics_summary(vault_id)` — high-level numbers (cluster count, deprioritization rate, lint pending)
- **Hermes:** session briefings include diagnostic summary

**6. Agent prompt text:**
```
memex_get_diagnostics_summary — Memory health overview for a vault. Use when the
user asks about memory state, when planning bulk curation, or when investigating
retrieval quality issues.

Returns: total units, deprioritized units, archived units, lint pending count by
type, retrieval-cluster count, average MW score, top-5 most-retrieved entities.

Visual diagnostics (manifold UMAP, heatmaps) are CLI-only; surface to user via
"run `memex diagnose manifold` to see the memory structure."
```

---

### TIER B — Consider after Tier S/A metrics

These have weaker evidence or higher uncertainty. Build only if Tier S/A metrics show a need.

| # | Feature | Source | Justification for Tier B |
|---|---|---|---|
| F3 | Layer formalization (docs only) | Issue #64 Comment 2 | Low ceiling; useful but not transformative |
| F7 | `memex lint review` CLI (interactive) | Issue #18 Comment 1 | UX nicety; non-destructive default makes it optional |
| F11 | FSFM-lite decay scoring (Ebbinghaus `exp(-t/S)` with importance-modulated stability) | **FSFM §4.5.1 explicit head-to-head** with pure Ebbinghaus baseline shows importance-weighted decay outperforms both pure Ebbinghaus and no-decay; theoretical foundation Ebbinghaus (1885), formal definition FSFM §3.1.1 ("Passive Decay-Based Forgetting"); also FSFM §2.1.2, §5.5 | FSFM's evidence is from telecom customer-service data; Memex content has long-tail value, not query freshness. Build only when reflection cron / lived experience shows retrieval is suffering from stale-bias. ZenBrain §H.1 NoDecay finding is about decay on top of an already-rich system (FSRS + Two-Factor + Sim-Select), not about importance-weighted decay alone. |
| F22 | Two-Factor edge confidence (variance) | ZenBrain §B.1 | Theoretically clean; needs validation data before investing |
| F24 | Reasoning-chain preservation | ZenBrain §B.12 (CoT Consolidation) | Useful for re-examination; schema-heavy |
| F27 | Comparative baseline registry | FSFM §4.5 | Useful once F23 exists |
| F30 | Distilled extraction-time classifier | D-MEM §7 | Cost optimization; only after F25 has labeled data |
| F31 | Embedding-similarity intent fallback | D-MEM §7 | Cheap alternative when LLM classifier unavailable |
| F35 | Non-stationary EMA mode for MW | MW §6 | Useful only if drift is observed |
| F36 | Outcome-confidence weighting in `record_outcome` | MW §7 | Cleaner signal; can ship as F1 v2 |

For each, the codebase touch points and prompt fragments would be derived from the related Tier S/A feature (e.g., F11 reuses F25 intent classes; F36 extends F1 API).

---

### TIER C — Skip with reason

| # | Feature | Why skip |
|---|---|---|
| F12 | Pre-write D-MEM RPE gate (full) | D-MEM §6.1 shows 53.9% real-turn skip rate; wrong for high-signal personal content |
| F13 | GDPR hard-delete | Build only when compliance requirement appears |
| F23 | Multi-judge retrieval eval harness | Enterprise-scale formalism; personal Memex doesn't justify 5-7w + ongoing LLM judge cost. User + daily reflection cron + spot-checks are sufficient signal. Reconsider only if multi-tenant deployment or research-claim publication becomes a goal. Lightweight alternative: pinned 20-30 query regression set, manually verified (~1w). |
| F21 | Sleep consolidation (full ZenBrain port) | 6–8 weeks for biological framing on top of mechanisms Memex already has via reflection + contradiction + cleanup; ZenBrain partially discredited; counterfactual replay is research, not foundation. Replaced by F38 (Tier A) at ~1 week. |
| F28 | TTL-based scheduled deprioritization | Subsumed by F25 intent classification + F11 decay |
| F29 | LoCoMo-Noise benchmark harness (Memex-internal) | F23 covers the core need; LoCoMo-Noise is overkill |
| F34 | Forced retrieval diversification | MMR already provides diversity; F33 exploration covers the rich-get-richer concern |
| — | TripleCopyMemory (ZenBrain B.8) | Overkill for personal scale; multi-timescale needs covered by F11 + F20 |
| — | NeuromodulatorEngine 4-channel (ZenBrain B.6) | Wildly out of scope; no use case |
| — | Bayesian Confidence Propagation (ZenBrain B.4) | Theoretically nice; hard to validate without F23 baseline |
| — | PriorityMap with emotion+goal (ZenBrain B.9) | Too rich for the evidence we have |
| — | MetacognitiveMonitor (ZenBrain B.11) | Bias detection is interesting but not core |
| — | Cross-vault entity unification | Entity is already global; unification is implicit |
| — | Adversarial memory-poisoning defense | Not urgent at personal scale |
| — | Multi-judge ensemble for reflection | F23 covers multi-judge for evaluation; reflection itself doesn't need this |

---

## 5. Implementation roadmap

Eight waves (Wave 0 added in v6.8 per Apr 2026 review point 2). Each wave produces a deliverable that's observable via daily reflection cron + user spot-check (F23 multi-judge harness was originally Wave 1 — moved to Tier C in v6.7; personal-scale doesn't justify the formalism).

| Wave | Duration | Features | Deliverable |
|---|---|---|---|
| **0 — Pre-conditions (executed v6.9 → see [`WAVE-0-PREWORK.md`](./WAVE-0-PREWORK.md))** | ~2 days (revised from ~1w in v6.8) | All 21 adversarial findings re-verified against current source; specs locked: **archive keeps current destructive-cascade behavior** (Option A considered then reversed — F4 `memory_deprioritize` is the non-destructive verb), MW cold-start α=β=1 + no warm-up gate (§6 #13), F1b centralized via `apply_generic_filters`, additive-marginal MW formula. **No archive code change needed** (the originally-planned `cascade_to_models=False` revert is dropped). | Wave 1 starts from a re-verified baseline; F1a's migration knows what to do (incl. `access_count` removal); F4's prompts must clarify that archive is the destructive cleanup verb and `memory_deprioritize` is the non-destructive complement. |
| 1 — Foundation | 4-6 weeks | F1a, F1b, F1c (sequential, independently shippable per the F1 split), F2, F33 | MW data + signal live; embedding anisotropy fix; exploration prevents rich-get-richer. F1a alone is the smallest reverble unit; F1b/F1c stack on top. |
| 2 — Write-time judgment | 3-4 weeks | F25 (+F26 subsumed) | Intent + risk classification at extraction; foundation for principled decay |
| 3 — Agent control | 4-5 weeks | F4, F5, F8 | Agent can deprioritize, summarize, query lint state. §3.5 walkthrough shows F1a + F1c + F4 already handle "issue resolved → drop from briefings" without new features. |
| 4 — Linter + orchestration | 5-7 weeks | F6, F10, **F38** | Self-healing maintenance ledger with surprise-gated LLM checks; consolidation orchestrator runs reflection + contradiction + cleanup nightly |
| 5 — Procedural observations | ~1 week | F14 (revised v6 — KV namespace, *not* SKILL.md; observations only, agent owns the procedure) | Cross-agent procedural observations queryable via Hermes briefings |
| 6 — Active learning | 3-4 weeks | F20 | FSRS revisitation; Memex schedules its own re-reads (F21 dropped — see Tier C) |
| 7 — Diagnostics | 3-4 weeks | F32, F9 | Memory becomes observable; reconsolidation closes the loop |

Total: roughly **22-30 weeks** for full Tier S+A including Wave 0 (back to v6.7 numbers after Wave 0 shrank from ~1w → ~2 days in v6.9). Tier S alone (Waves 0+1+2) is **7-10 weeks** and produces an observably governed system.

**Tight MVP** is now **F1a + F2 + F4** rather than the v6.7 "F1 + F2 + F4" — F1a alone is shippable and gives the deprioritization column F4 needs, plus the outcome write API; F1b/F1c can follow once the column is in production. ~5-7 weeks.

Recommended sequencing freedom:
- F2 can ship anytime (no dependencies) — could ship in parallel with Wave 0
- F1a → F1b → F1c is the recommended order but each is independently revertible; F4 only requires F1a
- F23 dropped (v6.7); a lightweight pinned-query regression set (~1w) can be added per-wave if a specific change feels risky
- F32 can ship incrementally alongside F1a / F6
- F14 (procedural) is somewhat independent; can run parallel to F4/F5/F8 if a second engineer is available

---

## 6. Open decisions

1. **What is "success" for `memex_record_outcome`?** Initial proposal: agent-explicit, called when a downstream action confirms the retrieved unit was load-bearing. F1a.
2. **What query-intent heuristics drive `include_deprioritized` flipping?** Initial: keyword triggers + explicit MCP param. F1b.
3. **Soft-factor threshold and sigmoid steepness for MW.** Start `threshold=0.5`, `sigmoid_k=10`; tune by spot-check + reflection-cron observation (F23 deprioritized v6.7). F1c.
4. **Beta-Bernoulli prior strength.** v6.8 picks α=β=2 (see #13 below); revisit if "memories are usually useful" prior fits data better. F1a.
5. **Bootstrap heuristic for existing units.** Superseded by #13 below — explicit warm-up gate replaces the `success_co_count = retrieval_count` heuristic. F1a/F1c.
6. **Single uvicorn worker (v1) or distributed locking now?** If multi-worker is on the roadmap, F9's per-entity advisory lock is required earlier.
7. **Decay parameters per-vault or global?** Per-vault override on global default. F11.
8. **Procedural validation policy.** Auto-promote a procedure to high-confidence after N successful follows? After explicit user ratification? F14.
9. **FSRS revisitation surface.** Hermes briefings, dedicated CLI command, or background MCP tool? F20.
10. **Diagnostic UI**: ship CLI-only (JSON output), or build a web view in `packages/web`? F32.
11. **AgeMem / CraniMem / HiMem (cited in #53)** — not yet reviewed; may shift Wave 5 design.
12. **Archive semantics — RESOLVED (v6.9 final): keep current destructive-cascade behavior; F4 `memory_deprioritize` is the designated non-destructive verb.** Wave 0 first considered three options:
    - (a) Make archive non-destructive on entity graph — skip `prune_stale_evidence` for archive (still mark units stale, but preserve MentalModels); only `supersede` keeps the cascade. Implementation: `cascade_to_models=False` parameter on `_deactivate_note_units`.
    - (b) Make archive fully destructive (also delete FileStore bytes).
    - (c) Document current behavior as intentional ("graph-destructive, disk-preserving").
    - **v6.9 first picked (a), then reversed to (c) on further review.** The reversal reasoning: F4 (`memory_deprioritize`) was greenlit specifically as the non-destructive curation verb (boolean flag, no cascades). If archive is *also* non-destructive, F4 is redundant. The cleaner design is two distinct verbs with distinct intents — **archive = "I'm done; clean up the graph"; deprioritize = "keep around, lower the weight"** — rather than one ambiguous verb. Empty MentalModels whose only evidence comes from archived notes are epistemically empty; `prune_stale_evidence` removing them is the correct behavior. The "stale evidence in MentalModels" risk that motivated Option A is moot once we accept the cascade as intentional. Restoration path for the rare archive-then-undo case: re-ingest the note + re-reflect (synthesis is derivable from evidence, not load-bearing state).
    - **Human-gating retained for bulk archive** (≥10 notes in one operation) precisely because the cascade is destructive — a misapplied bulk archive can vaporize a substantial fraction of a vault's synthesis in one call, and re-reflecting the surviving evidence is non-trivial. Single-note archive is still agent-autonomous (auditable, low blast radius). See §3.1 operation taxonomy for both rows.
    - **No code change** owed by Wave 0 (the originally-planned `cascade_to_models=False` revert is dropped). F4's prompt-text and `memory_deprioritize` tool description must explicitly contrast with archive: "use deprioritize when the unit should remain queryable but ranked lower; use archive when the note is genuinely done and the synthesized models built from it should be cleaned up too."
13. **MW cold-start strategy for existing units (Mn4) — RESOLVED (v6.9): Beta-Bernoulli `α=β=1` (uniform prior); no warm-up gate.** Wave 0 verification (`WAVE-0-PREWORK.md` §5) showed that the v6.8 worry about "every existing unit treated identically" only applied to a multiplicative `× sigmoid(MW − threshold)` formula — which would zero out cold-start units. v6.9 switches to the additive-marginal formula (see §3.4 below and F1c), where `α=β=1` → `mw_score = 0.5` → `mw_boost = 1.0` (perfectly neutral). New units pay no penalty; the boost activates organically as outcome data accumulates. No backfill, no warm-up gate, no `success_co_count = retrieval_count` heuristic. Wave 0 also commits to **removing the dead `MemoryUnit.access_count` column** (Mn8 — verified never written) as part of F1a's migration; if a retrieval counter is ever needed, it should be vault-scoped and outcome-aware, not a half-built column.

---

## 7. Cross-cutting concerns

### 7.1 Test impact

Tier S features touch heavy-coverage modules. Concrete file counts (measured 2026-04-29 against current `main`):

| Surface | Test files touching it | Notes |
|---|---|---|
| Total `packages/core/tests/` files | **259** | Baseline |
| Files referencing `MemoryUnit` | **108** | F1a column additions are nullable + new write path → most pass unchanged; expect ~10-20 to need DTO-aware updates |
| Files referencing `retrieval` / `strategies` | **117** | F1b parameterization defaults to current behavior → most pass unchanged; new `tests/integration/test_int_retrieval_scope.py` (NEW) covers the 4 scope combinations |
| Files referencing `ContentStatus` / `STALE` / `ACTIVE` | **17** | Status enum is unchanged in v6.8 (we use `is_deprioritized` boolean instead) → these should pass unchanged |
| Files referencing `rerank` / `reranker` | **74** | F1c attaches multiplicative factor → composition tests (NEW) verify (a) MW-undefined units pass through unchanged, (b) ranking properties hold |

**Rough migration cost per Tier S PR:** F1a ≈ 1-2 days of test updates (DTO + outcome write tests, mostly net-new); F1b ≈ 2-3 days (scope-matrix coverage); F1c ≈ 2-3 days (composition properties + edge cases). F2 ≈ 1 day (anisotropy is a pre-processor — affects similarity values but not test structure). F33 ≈ 1-2 days (exploration injection logic + outcome accounting).

**Modules requiring focused review** (high overlap with Tier S surfaces):
- `packages/core/tests/integration/test_int_retrieval_*.py`
- `packages/core/tests/integration/test_int_contradiction.py`
- `packages/core/tests/integration/test_int_deferred_reflection.py`
- `packages/core/tests/integration/test_int_overwrite_stale_units.py`
- `packages/core/tests/integration/test_lineage_regressions.py`
- `packages/core/tests/integration/test_note_delete_evidence_cleanup.py`
- `packages/core/tests/integration/test_vault_cascade.py`
- `packages/core/tests/unit/memory/contradiction/`
- `packages/core/tests/unit/memory/reflect/`
- `packages/core/tests/unit/memory/retrieval/`

Wave 0 verifies the above counts against `main` immediately before Wave 1 to catch any drift since this report was written.

### 7.2 Migration scale

MemoryUnit table is the largest. Audit row count before adding columns; stagger migrations if needed.

### 7.3 Multi-tenancy invariants

All MW counters and Procedure rows must carry `vault_id`. Entity stays global; cross-vault MW would leak signal across tenants.

### 7.4 LLM cost

F5, F9, F10, F25 all add LLM-intensive paths. Per-vault rate limits + cost telemetry essential.

### 7.5 Hermes plugin compatibility

The `memex_hermes_plugin` Stream-1 surface exposes **8** tools (verified in `packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py`): `memex_memory_search`, `memex_note_search`, `memex_survey`, `memex_add_note`, `memex_append_note`, `memex_list_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences`. Full schema across all streams is ~36 tools.

F1 scope params, F4 deprioritize, F8 lint flags, F14 skill retrieval (`memex_list_skills`, `memex_get_skill`, `memex_record_skill_use`), and F20 review prompts all need to be reflected in Hermes briefings and tool descriptions. Coordinate plugin updates with each feature wave.

---

## Appendix A: source materials

### Issues
- [#18 RFC needed — Self-healing wiki linting](https://github.com/JasperHG90/memex/issues/18)
- [#53 Feature: Agentic Memory Management (Unified LTM/STM Control)](https://github.com/JasperHG90/memex/issues/53)
- [#64 Feature: Implement Cognitive Memory Architecture (Selective Forgetting & Hierarchical Layers)](https://github.com/JasperHG90/memex/issues/64)

### Papers cited in #64 (foundation)
- ZenBrain — A Neuroscience-Inspired 7-Layer Memory Architecture for Autonomous AI Systems (arXiv:2604.23878v1)
- FSFM — A Biologically-Inspired Framework for Selective Forgetting of Agent Memory (arXiv:2604.20300v2)
- D-MEM — Dopamine-Gated Agentic Memory via Reward Prediction Error Routing (arXiv:2603.14597v1)
- Memory Worth — When to Forget: A Memory Governance Primitive (arXiv:2604.12007v1)

### Papers cited in #53 (not yet reviewed; flagged for Wave 5 design check)
- AgeMem — Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management (arXiv:2601.01885v1)
- CraniMem — Cranial Inspired Gated and Bounded Memory for Agentic Systems (arXiv:2603.15642v1)
- HiMem — Hierarchical Long-Term Memory for LLM Long-Horizon Agents (arXiv:2601.06377v1)

### Production reference
- @kinthaiofficial comment on #64 — anonymous; treat as design intuition, not evidence

### Bot reviews (referenced for code locations)
- github-actions[bot] on #53 (2026-04-26)
- github-actions[bot] on #64 (2026-04-28)

### Memex vault notes (cross-referenced for grounding)
- `2f21fd83` — MEMEX-DESIGN-DOCUMENT (§7.3 "Experiential Memory" gap)
- `56f98a0e` — Memex Design Document: Architecture Analysis, SOTA Comparison & Improvement Roadmap
- `a1472a80` — Feature Request: Cross-System Gap Analysis (Apr 2026 Batch) — establishes P0 security / P1 SKILL.md / P1 query routing / P3 consolidation refinements as the preceding roadmap
- `edbe3e4b` — Feature Ideas: Lessons from LLM Wiki, MemPalace, and MCP Critique (predecessor proposal)
- `d9ca8ba7` — ANALYSIS-codex-memory (Codex SKILL.md pattern, two-phase pipeline, usage-based retention)
- `9698821b` — ANALYSIS (architectural taxonomy of agentic memory systems including Hermes SKILL.md)
- `cb135f4f` — REVIEWED (Apr 2026 vault-survey of competing systems)
- `5992f782` — A Neuroscience-Inspired 7-Layer Memory Architecture (ZenBrain note)
- `c3147b35` — When to Forget: A Memory Governance Primitive (Memory Worth note)
- `b7c168ca` — LLM Wiki v2 (4-tier consolidation hierarchy)
- `156c4f72` — agentmemory README (4-tier consolidation reference implementation)

---

## Revision history

- **v1 (2026-04-28):** Initial report. Memory PR architecture with human-in-the-loop gating for all curation operations.
- **v2 (2026-04-28):** Adopted KinthAI's non-destructive principle. Human gate reserved for hard-delete. Explicit-recall retrieval scoping (§4) and vault-scope semantics for the linter (§5). F4 renamed to `memory_deprioritize`. F7 demoted to optional. F1 expanded to include retrieval-side soft-factor blending and scope parameters.
- **v3 (2026-04-28):** Incorporated adversarial review (Appendix B). Six critical fixes: MW counters off global Entity onto vault-scoped tables; orthogonal `is_deprioritized` column instead of status enum extension; explicit `memex_record_outcome` API replacing implicit session attribution; lint runs under existing single leader (not separate lock); `archive` reclassified as destructive; `memory_deprioritize` does not call `prune_stale_evidence`. F1 risk to Moderate, F5 effort to Very Low.
- **v4 (2026-04-29):** Full paper scan. Added 11 new features from material previously left on the table — most notably F14 (Procedural memory layer), F23 (Multi-judge eval harness), F25 (Write-time importance + intent classifier), F33 (MW exploration floor), F20 (FSRS revisitation), F21 (Sleep consolidation), F32 (Memory diagnostics). Restructured into 4-tier feature catalog with 6-dimension breakdown per Tier S/A feature (citation, new code location, code adapted, impact, surface impact, agent prompt text). Added implementation roadmap (7 waves, 31-41 weeks total) and cross-cutting concerns section. Tier S alone (~9-12 weeks) delivers measurable governance.
- **v5 (2026-04-29):** Vault-grounded corrections after deep memex-vault scan. Added §2.4 honest gap analysis vs existing services (correcting prior implicit overclaims about reflection/cleanup/contradiction coverage). Cross-referenced existing roadmap notes: Apr 2026 cross-system feature batch (`a1472a80`), MEMEX-DESIGN-DOCUMENT §7.3 "Experiential Memory" (`2f21fd83`), LLM Wiki / MemPalace prior proposal (`edbe3e4b`). **F14 rewritten** to use SKILL.md filesystem pattern (Codex/Hermes — vault note `d9ca8ba7`), aligned with existing Memex P1 roadmap entry; effort revised down (4-6w → 3-4w) since pattern is well-established externally; KV substrate proposal dropped (KV is global, wrong scope). **F21 demoted to Tier C**, replaced by **F38 — Consolidation orchestration** (Tier A, ~1 week) — a thin nightly orchestrator over existing reflection + contradiction + cleanup; captures 80% of sleep-consolidation operational benefit at ~1/8 the cost. Wave 4 absorbs F38; Wave 6 simplified to F20 only. §2.3 procedural row corrected ("absent — on roadmap as SKILL.md", not "covered by KV"). 4-tier consolidation hierarchy added to §2.3. §7.5 corrected: 8 Hermes tools, not 7. Total roadmap: 27-36 weeks (down from 31-41).
- **v6 (2026-04-29):** **F14 reframed again** — to KV-based procedural *observations*, not SKILL.md. New §2.3.1 articulates the layering principle: agent owns the procedure (its own skills system: Claude Code skills, Codex SKILL.md, Hermes SKILL.md); Memex owns observations about how to adapt those procedures to specific contexts. Definition committed to: *"Procedural memory in Memex is persistent observations about how an agent should adapt its existing skills to specific contexts. It is not the skill itself."* F14 effort drops to ~1 week (KV namespace + Hermes briefing integration + F1 outcome extension). Wave 5 shortened to ~1 week. Disagreement with Apr 2026 batch (`a1472a80` P1) explicitly flagged — recommendation to revise that note. Total roadmap: 24-32 weeks (down from 27-36).
- **v6.1 (2026-04-29):** Made Ebbinghaus (1885) heritage explicit in F11 and F20 citations. Decision: do *not* add a separate Ebbinghaus feature — pure time-based decay is already covered by F11 (Tier B), and F20 (FSRS) is the modern descendant for active revisitation. ZenBrain's NoDecay ablation (§H.1) and the observation that Memex content doesn't decay on a nonsense-syllable curve both argue against over-investing in pure time-decay before F23 (eval) shows it helps. F1 (MW) is the complementary evidence-based mechanism — memories that don't get used in successful outcomes get downweighted for behavioral reasons, not temporal ones.
- **v6.2 (2026-04-29):** Corrected the F11 framing after re-reading FSFM §4.5.1. FSFM has an **explicit head-to-head benchmark** of FSFM (importance-weighted decay) vs pure Ebbinghaus baseline vs no-decay; FSFM wins. This *strengthens* F11's empirical foundation — the prior framing ("ZenBrain says decay doesn't help") was wrong, because ZenBrain tested decay on top of an already-rich system, while FSFM tested decay-with-importance as the primary mechanism. F11 stays Tier B but for a different reason: FSFM's evidence is from telecom customer-service data, not personal knowledge management; Memex's content has long-tail value (research notes, design docs), so the curve shape may be different. F23 (eval) must validate on Memex's actual content lifecycle before promotion. §2.1 paper-validity row updated to credit FSFM §4.5.1 properly.
- **v6.3 (2026-04-29):** **Added §3.4 — Retrieval-weight composition** as the fourth architectural principle. Makes explicit what was scattered: KinthAI principle, MW (F1), FSFM importance-weighted decay (F11), and exploration floor (F33) are *the same thesis* — continuous non-destructive retrieval-weight adjustment — implemented via orthogonal signal sources that compose multiplicatively at the reranker (`engine.py:1116`). Each signal is blind to what others see (MW = behavioral; FSFM = temporal × content; recency = pure time); they don't compete for primacy, they compose. §3.1 updated to lead with KinthAI's principle quote. §3.2 updated to point to §3.4 for composition. Architectural implications spelled out: don't build separate ranking pipelines, add new signals to the composition site, F23 measures composed quality not per-signal ablations.
- **v6.4 (2026-04-29):** **Walked back F39 + F4 extensions added earlier in v6.3.** User pushback: "shouldn't that already be fixed with the existing propositions? MW and FSFM type stuff?" — correct call. The cron-reflection-resolution scenario (§3.5) is handled by **F1's `record_outcome` + F4's `deprioritize` + §3.4 composition** with no new features. Removed F39 (resolved_at timestamp + mark_resolved tool); reverted F4 to single-unit + free-text reason (no scope parameter, no resolution_type enum). §3.5 reframed as a walkthrough demonstrating that existing primitives suffice. Lesson: when a concrete user scenario surfaces, first walk it through with existing primitives before proposing additions. Only escalate to new features if the walk-through actually breaks.
- **v6.5 (2026-04-29):** **Added §3.6 — Semantic vs episodic retrieval split** after user observation: "note search is conducted across chunks. Alternatively, we use note search primarily for episodic recall." The cleaner architectural framing is two-mode retrieval — `memex_memory_search` (semantic, respects deprioritization, governed by §3.4 composition) vs `memex_note_search` / chunk search (episodic, preserves historical record, immutable in default scope). Resolves the "tomorrow's cron will re-surface resolved issues via note_search" concern from §3.5: it's not an architecture problem, it's an agent-prompting problem — the cron must use semantic search for "current state." Also resolves the F37 conceptual confusion — note_search **is** the episodic mechanism; F37 collapses to "smarter time-bounded scoping" rather than a new TEMPR strategy. §3.4 scope clarified to "semantic retrieval only." §2.3 implications updated. §3.5 "not a gap" list extended (no note-level deprioritize, no rank degradation by deprioritized fraction).
- **v6.6 (2026-04-29):** Two corrections after user pushback. **(a) Memory_search vs note_search reframed** — not "semantic vs episodic" (too rigid). The actual split is "primary retrieval (handles semantic, temporal, entity-scoped) vs source-content drill-down with status traversal." Memory_search handles time-bounded queries via existing temporal strategy. Note_search returns chunks with `linked_units` metadata (status, deprioritization, contradicts/weakens) so the agent can interpret source text in light of unit lifecycle. This extends the existing memory_links pattern. **(b) F37 dropped as a TEMPR strategy** but a real gap surfaced: topic-less time-window synthesis ("what was happening two weeks ago") — fits neither memory_search (no topic anchor) nor note_search (no synthesis). Right shape: extend `memex_survey` with `after`/`before`/`reference_date` params. Plus API-symmetry fix: add `reference_date` to `memex_note_search` (memory_search has it; note_search doesn't). Both folded into F1's existing tool-extension scope, ~1 week additional. §3.6 rewritten with the corrected framing.
- **v6.7 (2026-04-29):** **F23 (multi-judge eval harness) demoted from Tier S to Tier C.** Personal-scale Memex doesn't justify enterprise-grade eval formalism: 5-7 weeks of work plus ongoing multi-LLM judge cost, with no proportionate user benefit when the user IS the judge and the daily reflection cron provides ongoing signal. Tier S shrinks from 5 features to 4 (F1, F2, F25, F33). Wave 1 shortens from 6-8w → 4-6w. Affected sections updated: §3.3 observability (replaces F23 with cron + spot-check), §3.4 composition (quality signal is composed retrieval observed via cron, not formal harness), §6 open decisions (MW threshold tuned by spot-check), implementation roadmap intro. F11 (decay) gate rephrased from "wait for F23" to "build when reflection shows stale-bias." Lightweight alternative if a specific change feels risky: pinned 20-30 query regression set, manually verified (~1w each time). Reconsider F23 only at multi-tenant or research-publication scale.
- **v6.8 (2026-04-29):** Folded the Apr 2026 cross-cutting review (4 recommendations) into the body. **(1)** Added an Appendix B finding-resolution table mapping each C/M/Mn finding to addressed/deferred/accepted-risk in v6.8 — six Verdict prerequisites are now either resolved in the report or scheduled for Wave 0. **(2)** Added Wave 0 (~1 week) for code-fact reverification, archive-semantics decision (§6 #12), MW cold-start spec (§6 #13), and test-impact quantification — total roadmap now 23-31w. **(3)** Split F1 into F1a (schema + outcome API + DTO + API symmetry, 1.5-2w), F1b (retrieval scope parameterization at 11 SQL sites + 2 entry points = 13 places, 1-1.5w), F1c (MW soft-factor at the reranker, ~1w) — F1a alone is shippable; F4 only requires F1a, so the tight MVP is now F1a + F2 + F4 (~5-7w). **(4)** Added §6 #12 archive-semantics mini-RFC committing to option (c) "graph-destructive, disk-preserving — documented" for v1; reconciled §3.1 operation taxonomy with §2.4 truth (no more "partially reversible" for archive). Plus three minor folds: §3.1 retrieval-scope matrix made explicit (4-row table for is_deprioritized × status combinations); §3.4 pipeline ordering made explicit (resolves M1 — scope filter → RRF → reranker composition → MMR strictly downstream); §2.3 working-memory row clarified (ContextVar is per-request, not per-session in the agent sense); F14 collision-resolution policy added (last-writer-wins + version + capped history); §7.1 quantified test impact with concrete file counts (~108 MemoryUnit, ~117 retrieval, ~17 status, ~74 rerank). No new features; substantial structural improvements to readiness for Wave 0 kickoff.
- **v6.9 (2026-04-29):** Wave 0 executed against current source — see [`WAVE-0-PREWORK.md`](./WAVE-0-PREWORK.md). Five v6.8 specs refined or flipped after verification: **(1) §6 #12 archive — first flipped Option C → Option A, then reversed back to Option C ("keep current destructive-cascade behavior")** on further review. The reversal: F4 (`memory_deprioritize`) was greenlit as the explicit non-destructive curation verb; if archive is also non-destructive, F4 is redundant. Cleaner design = two distinct verbs (archive = destructive cleanup; deprioritize = non-destructive downweight), not one ambiguous verb. **No archive code change** (the `cascade_to_models=False` revert from the Option-A plan is dropped). §3.1 Mark-stale + Archive rows reframe accordingly. **Single-note archive remains agent-autonomous** (auditable, low blast radius, restoration via re-ingest + re-reflect); **bulk archive (≥10 notes) keeps human-gating** because the destructive cascade can vaporize a substantial fraction of a vault's synthesis in one call. **(2) §6 #13 MW cold-start simplified** from `α=β=2` + warm-up gate to **`α=β=1` (uniform prior), no warm-up gate** — works because of (3). **(3) §3.4 + F1c MW formula corrected** from pure-sigmoid `× sigmoid(MW − threshold)` (which would zero cold-start units) to **additive-marginal `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)`** with `mw_alpha = 0.3`, matching the existing `recency_alpha`/`temporal_alpha` pattern at `engine.py:1140`. Cold-start units get `mw_boost = 1.0` (perfectly neutral). **(4) F1b scope shrunk** from "11 SQL sites + 2 entry points = 13 places" to "1 branch in `apply_generic_filters` + 1 request model + 1 MCP tool + 1 DTO = 4 surfaces" — Wave 0 verified that all 8 strategy sites already flow through the centralization point, and `document_search.py` (4 Chunk/Node sites) and `engine.py:1357` (virtual units) explicitly should not get the filter. Scope matrix in §3.1 reduced to `include_deprioritized × include_stale` (using existing `include_stale` flag rather than adding `include_archived`). **(5) F1a now includes `MemoryUnit.access_count` removal** — Wave 0 confirmed Mn8 (column never written), follow-up grep in v6.9 confirmed zero read-sites in `packages/`, and the one existing test (`test_api_lineage_downstream.py:135`) asserts the column is *absent* from the DTO so it passes unchanged. The only stale reference is `.dev-team-artifacts/.../final-report.html` (historical artifact, not a live consumer — flagged for archival update post-F1a). Plus housekeeping: Wave 0 duration shrank ~1w → ~2 days; total Tier S+A back to 22-30w; Appendix B finding-resolution table refreshed; F1c effort bumped 1w → 1.5w to budget for the additive-marginal benchmark.

---

## Appendix B: Adversarial Review (v2)

**Reviewer:** Independent adversarial review agent
**Date:** 2026-04-28
**Method:** Spot-checked code references; explored repo structure; questioned architectural and empirical claims.

### Critical findings (would block the plan)

**C1. The `Entity` line reference in F1 is wrong, and the plan's MW-on-Entity design ignores the fact that `Entity` is global, not vault-scoped.**
F1 cites `packages/core/src/memex_core/memory/sql_models.py:46` for `Entity`. Reality: line 46 is inside the `Vault` class (`description` field). `Entity` is at `sql_models.py:701`. More importantly, `Entity` has NO `vault_id` column (lines 711-756 — only `id, canonical_name, phonetic_code, entity_type, first_seen, last_seen, mention_count, retrieval_count, last_retrieved_at`). Vault-scoping happens at `UnitEntity` (`sql_models.py:853`) and `MentalModel` (`sql_models.py:118`). Putting `success_co_count`/`failure_co_count` on `Entity` means MW counters are global across all tenants — breaking vault isolation. Section 5 explicitly says governance is per-vault. The plan must either put counters on `UnitEntity` / `MentalModel` (per-vault) or accept that entity MW is a multi-tenant cross-leak — neither option is acknowledged.

**C2. There is no "deprioritized" status anywhere; F4's core mechanism does not exist and migration is non-trivial.**
`grep -rn 'deprioritized' /home/vscode/workspace/packages/` returns ZERO matches. `Note.status` is constrained at the DB level by `ck_notes_status` to `('active', 'superseded', 'appended', 'archived')` (alembic `012_note_archived_status.py:25-29`, schema `sql_models.py:283`). `MemoryUnit.status` uses `ContentStatus` enum with only `ACTIVE` and `STALE` (`sql_models.py:35-39`). Adding `deprioritized` requires a new alembic migration, a CHECK constraint change, an enum extension, and updates to `set_note_status`'s `valid_statuses` tuple at `services/notes.py:222`. F4 calls this "Low risk — already exists, just expose" — that's flatly wrong.

**C3. The retrieval pipeline filters `status == ACTIVE` at the SQL strategy layer, BEFORE RRF/reranker run. `include_deprioritized=true` is not a one-line addition.**
`memory/retrieval/strategies.py:90, 238, 721, 763, 980, 1004, 1210` and `engine.py:1357` and `document_search.py:341, 482, 638, 660` all hardcode `WHERE status == ACTIVE` (or `'active'`). `include_superseded` is implemented entirely differently — as a *confidence threshold filter* applied AFTER hydration (`engine.py:450-452`), not as a status filter. So the existing flag is not the model F1 claims. To make stale/deprioritized units retrievable, every strategy query needs to be parameterized — that's a rewrite of the retrieval layer's lowest layer. "Modeled on existing `include_superseded` flag" is therefore misleading.

**C4. The plan's "session"/"episode" concept does not exist as a retrieval-to-effect span.**
F1 proposes counter increments based on whether "any retrieved unit's note/entity is referenced in a downstream MemoryUnit or MentalModel update within the same session." `context.py:9-29` shows `session_id` is a `ContextVar` defaulting to `'global'`, set per-request via `set_session_id`. Notes carry the session they were *ingested* in (`sql_models.py:178-182`), not the session they were *retrieved* in. There is no agent-level session that spans a retrieval call and a follow-up extraction. Without a true episode boundary, the "success/failure" dual counter cannot be wired up correctly — at best it conflates ingestion sessions with retrieval episodes. The MW Theorem 4.1 stationarity assumption is also dead in this context: vault contents change continuously between request boundaries, so the proof does not transfer.

**C5. F6's "advisory-lock leader-elected, like reflection" misreads the scheduler.**
`scheduler.py:18` defines `MEMEX_LEADER_LOCK_ID = 5432789123456789` — one lock for the whole deployment. `run_scheduler_with_leader_election` (`scheduler.py:94`) acquires that single lock and, if leader, runs ALL periodic tasks (reflection, vault summary, KV TTL) inside one `AioClock` instance. There is no per-task leader election. Adding a "separate advisory-lock leader-elected" lint job is not a one-line change — it would require either inventing a second lock ID (and reasoning about split-brain across two leader processes) or registering lint as another `@clock.task` under the existing leader (in which case it's not "separate"). The plan needs to commit to one model.

**C6. `prune_stale_evidence` is parameterized for *deleted* units, not *stale/deprioritized* ones, and does not generalize as F4 implies.**
F4 cites `services/mental_model_cleanup.py:17` claiming the cascade "already exists, just expose." Reality: `prune_stale_evidence` is at line 21, and its parameter is named `deleted_unit_ids: list[UUID]` (line 24). The function strips evidence whose `memory_id` is in `deleted_set` and deletes models whose observations all become empty. Calling this from a "deprioritize" path is semantically wrong — the units still exist, they just shouldn't dominate retrieval. Pruning their evidence from MentalModels is destructive in spirit (the observations lose their support and may be deleted) and contradicts the "non-destructive" principle this plan was rebuilt around.

### Major concerns (would force significant replanning)

**M1. The plan's `final_rank = RRF × sigmoid(MW_weight − threshold)` formula doesn't match the existing pipeline.**
`memory/retrieval/engine.py:233-510` shows the pipeline: query expansion → embeddings → RRF (in DB) → confidence filter → reranker (cross-encoder, sigmoid-normalized at line 1116) with multiplicative `recency_boost × temporal_boost` (lines 1132-1140) → MMR diversity. RRF is *upstream* of the reranker; the reranker overwrites RRF order. Inserting `× sigmoid(MW)` would compound a fourth multiplicative factor on top of `sigmoid(reranker) × recency × temporal`, with no analysis of how that interacts with MMR's lambda parameter. The plan needs a real proposal for *where* in the pipeline MW lands and how it composes.

**M2. F1 conflicts with `Entity.retrieval_count` and the existing hybrid score.**
`services/entities.py:66, 90-94` already implements `Hybrid Score = 0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality`. Adding `success_co_count`/`failure_co_count` overlaps with `retrieval_count` semantically — is `retrieval_count` deprecated? Folded into `success_co_count`? The plan is silent. F1 says read counters in centrality calc but doesn't address how the existing hybrid score is rewritten or whether its callers (entity listing, MCP `memex_list_entities`) get migrated.

**M3. `set_note_status('archived')` is destructive in a way the plan denies — and the file-on-disk side is destructive in the opposite way.**
`services/notes.py:244-245` flips note status to `archived` and calls `_deactivate_note_units`, which marks all linked memory units as `stale` AND calls `prune_stale_evidence` (line 319), which can DELETE MentalModels whose observations become empty. So "archive" already removes data. Conversely, the FileStore side is the opposite: hard `delete_note` triggers `txn.delete_file(doc.filestore_path, recursive=True)` (`services/notes.py:943`) but `set_note_status('archived')` does NOT touch the FileStore. So archived notes accumulate file bytes forever — at scale this is a real cost the plan ignores. Either way, "archive = non-destructive" needs a precise definition; right now it lies on both sides.

**M4. Reflection serialization is process-local, not distributed.**
F5 and F9 both assume reflection is "advisory-lock leader-elected." Plan §3.1 implies reconsolidation needs "per-entity advisory lock, reuse the scheduler's pattern." But `services/reflection.py:55` uses `asyncio.Lock` — this serializes within ONE process only. Multiple API workers (uvicorn `--workers > 1`) can call `reflect()` concurrently on the same entity. F5/F9 need actual distributed locking; the plan claims to "reuse" a pattern that isn't there.

**M5. Test coverage on the affected modules is heavy — "low risk" claims for F1/F2/F4 ignore migration cost.**
`packages/core/tests/integration/` contains `test_int_contradiction.py`, `test_int_deferred_reflection.py`, `test_int_overwrite_stale_units.py`, `test_int_retrieval_nplus1.py`, `test_int_review_bugfixes.py`, `test_lineage_regressions.py`, `test_note_delete_evidence_cleanup.py`, `test_vault_cascade.py`, plus dozens of unit tests under `unit/memory/contradiction/`, `unit/memory/reflect/`, `unit/memory/retrieval/`. Changing `MemoryUnit` columns, retrieval scope semantics, or status enums will cascade into many of these. Calling F1 "Very low" risk requires either a test-impact analysis (absent) or a back-of-envelope of "X tests touching MemoryUnit signatures."

**M6. F9's `memory_reconsolidate` uses `ContradictionEngine.detect_contradictions` against entity-scope, but the function takes `unit_ids` — not entities.**
`memory/contradiction/engine.py:39-45` — signature is `(session_factory, document_id, unit_ids, vault_id)`. The function loads units from `unit_ids`, triages new ones, and detects contradictions against existing units in the same vault. F9 says "runs `ContradictionEngine.detect_contradictions()` followed by `ReflectionEngine.reflect_on_entity()` for a specific entity." That requires either (a) a new entity → unit_ids resolver step before calling detect_contradictions, or (b) a new method on `ContradictionEngine` that scopes by entity. F9 doesn't acknowledge either.

**M7. `services/reflection.py:29` is a class declaration, not a function to extend.**
F5 says "add synchronous path" at `services/reflection.py:29`. Line 29 is `class ReflectionService:`. The actual method is `reflect()` at line 88, which is *already* a synchronous-from-the-caller path (you await the result). The "synchronous" thing the plan wants is what `reflect()` already is; what's missing is the MCP exposure, not a new code path. Similarly, `server/reflection.py:32` already has `POST /reflections` returning `ReflectionResultDTO` synchronously — F5's "new endpoint" is already there.

### Minor concerns (worth noting)

**Mn1.** F1 cites `schemas.py:323` for "DTO mirror"; line 323 is `MemoryUnitBase` (the shared base), not `MemoryUnitDTO` (line 354).

**Mn2.** `reflection.py:69` is `class ReflectionEngine:` — not an entity-scoped method. `reflect_on_entity` exists at line 543 but is marked "Legacy wrapper" and is just `reflect_batch([request])`.

**Mn3.** `mental_model_cleanup.py:17` is `from memex_core.memory.reflect.queue_service import ReflectionQueueService` (a TYPE_CHECKING import); the actual function is at line 21.

**Mn4.** No backfill plan for MW counters on existing data. With existing units, `success_co_count = failure_co_count = 0` means MWT is undefined or 0/0; the soft-factor formula `sigmoid(MW − threshold)` will treat all existing memory equally. Need a bootstrap heuristic (e.g., seed `success_co_count = retrieval_count`) or accept a cold-start period that's not characterized.

**Mn5.** No observability spec for new mechanisms. Memex has Prometheus metrics (`metrics.py`) and OpenTelemetry tracing (`tracing.py`) — the plan adds an MW counter and a soft-factor in retrieval but proposes no metrics for them. The "is governance working?" question has no instrumentation answer.

**Mn6.** The plan's empirical foundation is shaky. Plan §1 says "ZenBrain §H.1 found disabling decay had negligible cost" — but Plan §2.1 also concedes ZenBrain "[is] discredited on retrieval-proper metrics (§F.1)." Using the same paper as authority for one claim and as untrustworthy for an adjacent claim is selective. The KinthAI material is cited as a "production reference" — it's an anonymous blog comment, not peer-reviewed evidence. MW Theorem 4.1 explicitly assumes stationarity; multi-tenant Memex with continuous ingestion is non-stationary, so the proof's "almost-sure convergence" doesn't apply.

**Mn7.** `Entity.retrieval_count` already provides a one-counter analog — bot review on #64 was right that `access_count` is *something*. Plan dismisses it via "MW §3.4 proves it's insufficient" but never addresses why the existing `mention_count + retrieval_count + centrality` formula isn't already a richer (if differently shaped) signal than MW's two scalars.

**Mn8.** F11 says it would add `last_accessed_at` to MemoryUnit — but `MemoryUnit.access_count` already exists at `sql_models.py:554` with no timestamp. The migration is straightforward, but the plan doesn't note that increments to `access_count` are not currently wired anywhere I could find — `grep -rn 'access_count.*+= 1\|access_count.*+ 1'` was empty. So the existing column is dead code, which weakens the "we already have access tracking" implicit assumption.

### Verdict

The plan does not survive review without significant rework. The core engineering thesis — "non-destructive curation via retrieval-weight adjustment" — is sound, but the proposal is built on at least four hard factual errors about the codebase (C1, C2, C3, C5) and one foundational concept gap (C4: no episode boundary). F1 (the Wave 1 keystone) cannot be implemented as specified: its line references are wrong, its target column placement breaks multi-tenancy, its "modeled on `include_superseded`" claim misrepresents how that flag works, and its success-signal assumes a session model the codebase does not have. F4's "deprioritized" status simply does not exist and requires schema work the plan calls "Low risk." F5 and F9 cite line numbers that point at class declarations and legacy wrappers, then claim functions need to be "added" that are already there.

Required before any code is written: (a) re-derive every `file:line` reference; (b) decide where MW counters live given Entity is global (`UnitEntity`? `MentalModel`?); (c) define a real "episode" — either a new agent-session ContextVar plumbed across retrieve→write, or an explicit feedback API; (d) audit every retrieval strategy query in `strategies.py` for the cost of plumbing a `scope` parameter; (e) decide whether `archived` is destructive (current code) or non-destructive (plan's intent) and either rewrite `_deactivate_note_units` or rewrite the plan; (f) commit to either single-leader or per-task-leader election in `scheduler.py`. Until those are answered, the effort estimates in §10 are not trustworthy.

### Finding-resolution table (added v6.8; refined v6.9 against `WAVE-0-PREWORK.md` source verification)

Status of each adversarial finding as of v6.9. **Addressed** = the body of the report now reflects the finding. **Deferred** = explicitly punted to a later wave with a named feature/decision. **Accepted risk** = considered and chosen not to mitigate, with reasoning in the body.

| # | Finding | v6.9 status | Where in report |
|---|---|---|---|
| C1 | `Entity` is global; MW on Entity breaks multi-tenancy | **Addressed** (v6.7) | F1a places counters on `UnitEntity` / `MentalModel` / `MemoryUnit`; §2.4 paper-validity row + §7.3 multi-tenancy invariant make this explicit |
| C2 | No `deprioritized` status anywhere; F4 is non-trivial | **Addressed** (v6.7 + split in v6.8) | New `is_deprioritized` boolean column on `MemoryUnit` (orthogonal to status enum); F1a ships the column, F1b parameterizes via `apply_generic_filters`, F4 only requires F1a |
| C3 | 11+ retrieval-strategy SQL sites need parameterization | **Addressed** (v6.9 verification: smaller than v6.8 implied) | All 8 strategy sites flow through `apply_generic_filters` — F1b is one branch in one function. `document_search.py` (4 Chunk/Node sites) explicitly NOT touched per §3.6. `engine.py:1357` virtual-unit construction NOT touched |
| C4 | No agent session for retrieve→write attribution | **Addressed** (v6.7) | Explicit `memex_record_outcome` API replaces implicit session attribution; §2.3 working-memory row updated v6.8 |
| C5 | `scheduler.py` has one leader, not per-task election | **Addressed** (v6.7) | F6/F38 register as `@clock.task` under existing `MEMEX_LEADER_LOCK_ID`; no new lock |
| C6 | `prune_stale_evidence` is for deleted units, not deprioritized | **Addressed** (v6.7) | F4 explicitly does NOT call `prune_stale_evidence`; sets only the `is_deprioritized` flag. Wave 0 (v6.9 final) keeps archive's existing call to `prune_stale_evidence` — the destructive cascade is intentional cleanup; F4 is the non-destructive complement. See §6 #12. |
| M1 | MW vs. existing reranker pipeline order undefined | **Addressed** (v6.9 — formula corrected) | §3.4 makes pipeline order explicit AND formula explicit: composition site is `engine.py:1140` (the boost line, not :1116); additive-marginal `mw_boost = 1.0 + mw_alpha × (mw_score − 0.5)` (NOT pure-sigmoid which would zero cold-start units) |
| M2 | F1 conflicts with `Entity.retrieval_count` + hybrid score | **Addressed** (v6.9) | F1a leaves `services/entities.py:66` UNCHANGED. `WAVE-0-PREWORK.md` §M2 documents the relationship: Entity hybrid score is per-entity popularity ranking; MW on `UnitEntity` is per-unit behavioral signal — different tables, different granularity, different semantics. No conflict |
| M3 | Archive destructive on graph + accumulating on disk | **Addressed** (v6.9 final — kept current behavior, reframed) | §6 #12 considered Option A then reversed: archive's destructive cascade is intentional cleanup; F4 (`memory_deprioritize`) is the non-destructive verb. §3.1 operation taxonomy reflects two distinct verbs (archive = destructive cleanup; deprioritize = non-destructive downweight). Bulk-archive (≥10 notes) human-gated to mitigate runaway-deletion risk. FileStore retention deferred to Tier B. |
| M4 | Reflection uses `asyncio.Lock`, not distributed | **Deferred** (to F9) | §2.4 acknowledges; F9 adds Postgres advisory locks per entity in `services/locks.py` (Wave 7); F5 v1 explicitly accepts the multi-worker race |
| M5 | Test impact on F1/F2/F4 underestimated | **Addressed** (v6.8 quantified) | §7.1 + each F1 sub-PR: ~108 tests touch `MemoryUnit`, ~117 touch `retrieval`, ~17 touch `ContentStatus`, ~74 touch `rerank`. F1a/F1b/F1c each scope their test impact concretely |
| M6 | F9's contradiction call needs entity → unit_ids resolver | **Addressed** (v6.7) | F9 includes the resolver step explicitly |
| M7 | `services/reflection.py:29` is a class, not a function | **Addressed** (v6.7) | F5 corrected to delegate to existing `reflect()` and `POST /reflections`; no new code path |
| Mn1 | `schemas.py:323` is base, DTO is at :354 | **Addressed** (v6.7) | F1a cites `schemas.py:354` |
| Mn2 | `reflection.py:69` is class; `reflect_on_entity` is legacy | **Addressed** (v6.7) | F9 uses `reflect_batch` directly |
| Mn3 | `mental_model_cleanup.py:17` is import; function at :21 | **Addressed** (v6.7) | §2.4 cites correct line |
| Mn4 | No backfill plan for MW counters on existing units | **Addressed** (v6.9 — simplified) | §6 #13: Beta-Bernoulli `α=β=1` (uniform prior). With additive-marginal MW formula, cold-start `mw_score = 0.5` → `mw_boost = 1.0` (neutral). No warm-up gate, no backfill, no `success_co_count = retrieval_count` heuristic (latter would have leaked global Entity signal cross-tenant) |
| Mn5 | No observability spec for MW counters | **Addressed** (v6.8) | F1a adds Prometheus `memex_outcome_recorded_total` + `memex_mw_score_distribution`; F1c adds `memex_mw_boost` histogram |
| Mn6 | Empirical foundation cherry-picks ZenBrain | **Accepted risk** | §2.1 paper-validity table is explicit about cherry-picking; production signal is reflection cron + spot-checks (§3.3); revisit at multi-tenant scale |
| Mn7 | `Entity.retrieval_count` already provides analog signal | **Accepted risk** (with documentation) | F1a leaves entities.py UNCHANGED. `WAVE-0-PREWORK.md` §M2/Mn7 documents the orthogonality: Entity hybrid score for entity ranking; MW on `UnitEntity`/`MemoryUnit` for unit retrieval ranking. Different tables, different signals |
| Mn8 | `MemoryUnit.access_count` is dead code | **Addressed** (v6.9 — scheduled for removal, grep-verified) | F1a's migration removes the column + index. v6.9 grep confirmed: zero write-sites beyond `extraction/storage.py:73` initializer (also dropped); zero read-sites anywhere in `packages/`. Existing test `test_api_lineage_downstream.py:135` asserts the column is *absent* from the DTO → passes unchanged. Only stale reference is `.dev-team-artifacts/.../final-report.html` (historical artifact, flagged for archival update). |

**What's resolved by Wave 0 (NEW v6.8):**
- Re-derive every `file:line` reference (Verdict point a) — Wave 0 audit
- Decide where MW counters live (Verdict point b) — already done in v6.7, Wave 0 verifies migration code matches
- Define real "episode" (Verdict point c) — explicit feedback API chosen v6.7, Wave 0 verifies MCP signature
- Audit retrieval scope plumbing cost (Verdict point d) — F1b enumeration in v6.8 + Wave 0 verifies line numbers
- Decide archive semantics (Verdict point e) — §6 #12 commits to (c) for v1
- Commit single vs per-task leader (Verdict point f) — already done in v6.7 (single leader, register as `@clock.task`)

All six Verdict prerequisites are now either resolved in the report or scheduled for Wave 0 verification before Wave 1 starts.
