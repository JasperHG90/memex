# Design principles (P1–P13)

A scenario from a real Memex session: you ingest a meeting note in January saying the team picked Postgres. In April you ingest a follow-up saying they switched to SQLite. The agent asks Memex what database the team uses, and Memex returns the April fact — but the January one is still there, still searchable with `apply_pre_filter=False`, still part of the audit trail. Nothing was deleted. The January claim was downweighted, not erased.

That outcome is not an accident. It is what falls out of thirteen design principles applied consistently. This page explains those principles — what each one says, why it exists, and where it lives in the code.

## Why principles matter

A complex system grows from thousands of small decisions. Without anchoring rules, those decisions drift apart and the system becomes incoherent. Memex has thirteen anchoring rules. They answer questions like:

- When the user asks to forget a fact, do you delete it or downweight it?
- When a contradiction lands, do you overwrite the old fact or version it?
- When the LLM extraction service goes down, does retrieval fail or fall back?
- When an entity shows up in two vaults, is it one row or two?

Each principle settles one of these recurring questions once, so the answer does not have to be re-litigated every time a new feature touches the same area.

Two flavours of principle, and the difference matters. **Invariants** are structural properties of the system. They have a CI-enforceable check at a verified location in the code — break the invariant and a test fails before your PR merges. **Values** are positioning choices that shape design taste but cannot be mechanically verified. Treat values like a senior engineer's instinct: they tell you which direction to lean when the trade-off is open, but they do not catch you when you slip.

The practical use of the split is this: when a code review feels off, check the invariants first — they are precise, and if a patch breaks one, that is usually the whole story. When a design feels off but you cannot point at a broken rule, check the values — the patch may sit inside every invariant and still drift away from how the system wants to be built. The invariants are the guardrails. The values are the shape of the road inside the guardrails.

The next three sections lay out the principles in a single table, then drill into the mechanism that enforces each invariant and one concrete decision that flows from each value.

## A note on calibration

The principles describe how the system is built. They do not claim every default value inside the system is calibrated. Memex ships with literature-derived defaults — `mw_alpha=0.3`, `cooldown_days=14`, the four FSFM composite weights, an entity-resolver threshold of 0.65 — that have not been fine-tuned against held-out user data. The principles guarantee the *shape* of composition (P3), the *separation* of write-time from retrieval-time signal (P5), and the *observability* of every knob (P13). They do not guarantee any specific knob value is optimal.

Empirical characterization — sensitivity sweeps over the knobs, calibration of `mw_score`, ablation deltas per composition factor — is pending. The only evaluation surface today is the hand-verified regression set under `packages/eval/`, which catches obvious regressions but cannot tune the system's many knobs without overfitting.

What this means for a reader: the principles are stable; the numeric defaults are first-pass estimates. When you tune a knob in your deployment, you are doing work the upstream project has not yet done in your domain, and the histograms P13 demands are how you tell whether the tune is helping.

## What the principles are not

Before the list, a note on scope. The principles are *not* a feature checklist, and they are *not* an architectural diagram. They sit one level above both.

A feature checklist tells you what the system does — supports vector search, runs reflection nightly, ships a Claude Code plugin. The principles do not list features; they explain why the system says yes to some features and no to others. "Does Memex support hard delete by default" — no, because P1. "Does Memex let two vaults share a synthesized mental model" — no, because P7.

An architectural diagram tells you what the parts are and how they connect — FastAPI in front of services, services in front of memory engines, memory engines in front of storage. The principles do not draw those boxes; they tell you why the boxes are arranged that way. The retrieval path has no LLM in the hot loop because P2. The reflection scheduler runs against a queue rather than synchronously because P4.

You can read the architecture overview to see what is built. You read this page to understand why it was built that way.

## The thirteen principles

| ID  | Principle                                                           | Kind      |
|-----|---------------------------------------------------------------------|-----------|
| P1  | Non-destructive curation; the user has the last say                 | Invariant |
| P2  | Memory extraction is a data problem (write-heavy, read-optimized)   | Value     |
| P3  | Multi-strategy fusion and signal composition                        | Value     |
| P4  | Reflection as a first-class operation                               | Value     |
| P5  | Separate write-time judgment from retrieval-time scoring            | Invariant |
| P6  | Append-only, with strict lineage and traceability                   | Invariant |
| P7  | Vault-scoped multi-tenancy with one global noun-graph               | Invariant |
| P8  | Orthogonal to LLM reasoning                                         | Value     |
| P9  | Strict LLM contracts                                                | Invariant |
| P10 | Model-agnostic ("batteries included")                               | Value     |
| P11 | Local-first, open-source, zero lock-in                              | Value     |
| P12 | Graceful degradation of systems                                     | Invariant |
| P13 | Memory must be observable                                           | Invariant |

Seven invariants, six values. The invariants are not "more important" — the system needs both kinds — but the invariants are the ones a check-in test can defend.

## The seven invariants and their enforcers

Each invariant below names the mechanism that makes it real. The citation points at the file and lines that hold the line. Line numbers drift; the path is the anchor.

You will see two shapes recur. Some invariants are enforced by the schema layer — a CHECK constraint or a column type that the database itself refuses to violate. Others are enforced by a unit test that asserts a structural property the schema cannot express directly (for example, "this column is not present on this table"). Either way, the enforcer is a check that runs without human attention; a PR that breaks it gets red.

The "Why this matters" subsection for each invariant names the failure mode the principle prevents. If the rule sounds abstract, picture the failure mode and reverse-engineer the rule from it — that is the reading order the system was designed under.

### P1 — Non-destructive curation; the user has the last say

The rule: Memex gracefully degrades information, but never deletes it without explicit human permission. When you mark a memory as wrong, the system lowers its retrieval score and hides it from default queries — it does not unset a row. Archive is the same story: the note row, the synthesized graph, and the outcome counters all survive. Only hard deletion, gated behind an explicit human-confirmed verb for GDPR, removes data.

Why this matters: a memory system that silently destroys data is a memory system you cannot trust. The first time you ask "what was the rationale we ruled out last quarter" and the answer is gone, the system has betrayed its purpose. Non-destructive curation costs storage; it saves the audit trail.

The enforcer is a check constraint at the schema layer. The `Note.status` column accepts only `active` or `superseded`. Anything that wanted to express "archived" or "appended" as a terminal state would have to add a column or break the constraint — and the test suite would notice.

<code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="343-346" />

### P5 — Separate write-time judgment from retrieval-time scoring

The rule: importance, intent, and risk are decided once at ingest and stored as columns. Outcome signal accumulates lifelong via `memex_record_outcome`. The reranker composes both at query time and never writes back into the write-time columns.

Why this matters: if recall mutated the labels it composed with, the audit trail would erase itself with every query. You would never be able to ask "what was the original importance the classifier assigned" because a later retrieval would have shifted it. Separating the two clocks (write-time judgment, retrieval-time composition) keeps each clock honest.

The enforcer is the column layout on `MemoryUnit`. `intent_class`, `risk_class`, and `importance` are write-time fields. The retrieval composition chain reads them as inputs and never writes them back.

<code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="643-653" />

### P6 — Append-only, with strict lineage and traceability

The rule: notes and memory units are immutable once created. A new version creates a new row, with a pointer to its predecessor. Lifecycle state (`active` / `superseded`) lets a query ask either "what does the system believe today" or "what did the system believe in March".

Why this matters: when an agent surfaces a claim, you should be able to trace it back to the specific paragraph of the specific note that produced it. If notes were mutable, the paragraph the claim cites might not still say what it said when the claim was extracted. Append-only makes lineage trustworthy.

The enforcer is the `superseded_by` column on `Note`. Combined with the upsert path, new versions create new rows pointing back at the prior id. The old row is never overwritten.

<code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="303-307" />

### P7 — Vault-scoped multi-tenancy with one global noun-graph

The rule: memory tables are scoped to a vault for project isolation, but the `Entity` table is global. A person mentioned in your work vault is the same person mentioned in your personal vault. The synthesized *understanding* of that person is vault-scoped — Alice the colleague and Alice the neighbour may have entirely different mental models — but the canonical noun is shared.

Why this matters: if entities were vault-scoped, cross-project lineage would be impossible. You could never ask "everywhere Alice has come up across all my work". If mental models were global, the work-Alice mental model would pollute the personal-Alice one. Splitting the noun from the understanding gets both behaviours.

The enforcer is a schema test that asserts the `Entity` table has no `vault_id` column. Vault-scoped data lives on joins like `UnitEntity` and `MentalModel`, which carry the per-vault outcome counters and synthesized observations.

<code-ref path="packages/core/tests/unit/test_entity_schema_invariants.py" lines="4-7" />

### P9 — Strict LLM contracts

The rule: every internal LLM call inside Memex is bound to a DSPy `Signature` subclass with Pydantic-typed input and output fields. The contract is the signature class, not the prompt text. Swap the model backend, the signature still holds.

Why this matters: prompts drift between model versions. A prompt that produced clean JSON on one model produces prose on another. Pinning the contract at the type layer means the structured output is checked at runtime by Pydantic — bad output raises, not silently corrupts the pipeline.

The enforcer is structural: production LLM call sites live behind a `dspy.Signature` subclass with Pydantic field types. Twelve modules under `packages/core/src/memex_core/` carry signatures spanning extraction, classification, contradiction triage, reflection, retrieval, and processing. One example among twelve:

<code-ref path="packages/core/src/memex_core/memory/extraction/signatures.py" lines="16-44" />

### P12 — Graceful degradation of systems

The rule: every optional component fails open. A retrieval with a broken reranker still returns results — just unranked. Query expansion with a flapping LLM falls back to identity tokenization. The surprise-gated lint, if its model is unreachable, skips the gate rather than blocking the run.

Why this matters: external LLM endpoints are flaky. A memory system that fails closed on every transient blip is unusable. The alternative — propagating the failure — would mean every agent turn that touched a degraded component fails, even when 90% of the request could have succeeded.

The enforcer is the `CircuitBreaker` class wrapped around every LLM call site. On a configurable failure threshold the breaker trips open. The calling code path catches the trip and falls back to its degraded default (raw cosine for the reranker, identity tokenization for expansion).

<code-ref path="packages/core/src/memex_core/circuit_breaker.py" lines="42-49" />

### P13 — Memory must be observable

The rule: you cannot tune what you cannot see. Every mechanism in Memex emits Prometheus counters, histograms, and gauges; spans land in OpenTelemetry; diagnostic UMAPs and heatmaps ship in-product.

Why this matters: tunable knobs without telemetry are landmines. You set `mw_alpha=0.3` because the literature suggested it, and you have no idea whether it is helping or hurting. Observability turns "we believe this is calibrated" into "we have the histograms to prove it" — or to disprove it.

The enforcer is twofold. The Prometheus registry declares counters, histograms, and gauges for ingestion, retrieval, and reflection. New mechanisms are expected to register here. OpenTelemetry tracing is wired through `setup_tracing()` and instruments the same paths.

<code-ref path="packages/core/src/memex_core/metrics.py" lines="11-22" />
<code-ref path="packages/core/src/memex_core/tracing.py" lines="24-32" />

## The six values and how they show up

Values do not have enforcers. They show up as a pattern of decisions across the codebase. For each, here is the rule, the reason behind it, and one concrete choice that flows from the value — the kind of thing you would point at if someone asked "where does this principle live?"

### P2 — Memory extraction is a data problem

The rule: writes are heavy and async; reads are fast. Entity resolution, fact extraction, contradiction detection, and risk classification all happen during ingest, behind a per-vault rate limiter. Reads then resolve through pre-computed HNSW, GIN, and B-tree indexes — no LLM in the hot path.

The reason: an agent calls the memory system on every turn. If retrieval cost an LLM call, the latency would compound and the user would feel it. Pushing work to write time costs nothing from the agent's perspective — the writer was already asynchronous — and saves the read path.

A concrete decision: the entity resolver runs at write time against trigram + Double Metaphone + TF-IDF, not at read time against the query. The retrieval-time cost is a single SQL lookup against pre-resolved canonical IDs.

### P3 — Multi-strategy fusion and signal composition

The rule: no single retrieval signal beats the others. Memex fuses five orthogonal strategies (Temporal, Entity, Mental-Model, Keyword, Semantic) via Reciprocal Rank Fusion, then composes ranking signals (recency, temporal proximity, Memory Worth, confidence variance, FSFM decay) at a single bounded log-additive chain.

The reason: every retrieval strategy has a failure mode. Pure semantic search misses temporal anchors. Pure keyword search misses paraphrases. Pure entity traversal misses queries with no entity hits. Fusion makes the system robust to any one strategy's weakness; bounded composition prevents any one ranking signal from dominating.

A concrete decision: every new ranking signal must land at the same composition chain. Parallel scoring paths are rejected at review — they would let one signal dominate without the operator being able to see or bound it.

### P4 — Reflection as a first-class operation

The rule: raw facts are necessary but insufficient. A nightly orchestrator drains the reflection queue, synthesizing facts into mental models with trend tracking.

The reason: a user wants to ask "what does the system think about Alice's role on the project" and get a coherent answer, not seventeen fragments. Synthesis is structurally separate from extraction — extraction is per-chunk, synthesis is per-entity-across-chunks. Running synthesis on a cadence (rather than on every write) batches the work and keeps the foreground path cheap.

A concrete decision: reflection is a seven-phase loop with a separate enrich phase that retroactively tags contributing memory units — closing the loop that the Hindsight Framework did not address.

### P8 — Orthogonal to LLM reasoning

The rule: Memex augments LLM reasoning; it does not replace it. The system provides facts, lineage, and retrieved context. The LLM reasons over them.

The reason: an opinionated memory system that pre-baked answers would lock the user into the system's reasoning. If Memex returned "the team uses SQLite" instead of returning the April note that says so, the user could not see the January note that contradicts it — and the agent could not weigh the evidence. Surfacing structured data with citations preserves the user's ability to reason.

A concrete decision: the MCP surface returns structured data with inline citations, not pre-baked answers. The agent decides what to do with the evidence.

### P10 — Model-agnostic, batteries included

The rule: Memex works with any model LiteLLM supports, and ships purpose-built plugins for major agent harnesses (Claude Code, Hermes).

The reason: model lock-in is a long-tail risk. Today you use Sonnet. Tomorrow you want to evaluate Haiku for cost or Gemini for context length. A memory system that only works with one model would force a rewrite of every signature. Pinning the contract at the DSPy signature instead of the prompt text makes the model a config knob.

A concrete decision: there is no model-specific prompt anywhere in production. The DSPy signature is the contract; the model is a runtime choice.

### P11 — Local-first, open-source, zero lock-in

The rule: users own their memory. The data lives on the user's disk by default.

The reason: a vendor's promise to host your memory is a five-year bet that the vendor still exists, still charges what they said they would, and has not been acquired by someone whose priorities differ. Identity-shaped data — your preferences, your decisions, your accumulated context — should not depend on that bet.

A concrete decision: the FileStore writes notes as Markdown files on local disk (or S3, or GCS — your choice via fsspec). You can `git init` your vault and version it yourself. Export is a `cp -r` away.

## Trade-offs

A principle is interesting because it cost something. If a principle were free, no one would bother naming it — it would just be the default. The most informative principles are the ones where the alternative was attractive enough to consider, and the team chose the other path for specific reasons.

Four principles deserve a closer look. For each, picture the version of Memex that took the alternative path. Some features that exist would not exist; some failures the system avoids today would be everyday occurrences.

### Why P5 (separate write-time from retrieval-time) and not "update labels as you learn"

The alternative would let recall mutate the write-time labels. When a query revealed that a unit labelled "low importance" was actually load-bearing — surfaced and confirmed across many sessions — the system would bump its `importance` column.

That alternative loses the audit trail. The very first time you debug a bad answer by asking "what did the classifier think this unit was when it was ingested", the answer is no longer the classifier's answer — it has been overwritten by lifetime signal. You cannot tell whether the classifier was wrong or whether the unit's importance simply rose over time.

Memex makes the opposite choice. `importance`, `intent_class`, and `risk_class` are frozen at write. Lifetime signal accumulates in `success_co_count`, `failure_co_count`, and `is_deprioritized` — separate columns, separate clocks. The retrieval composition chain reads both and composes them at query time. You can always answer "what did the classifier think" and "what has the system learned since" independently, because they are stored independently.

The cost: the composition chain has more inputs to bound and observe. The benefit: every claim is debuggable back to its source decision.

### Why P12 (graceful degradation) and not "fail fast"

The fail-fast school says: if a component is broken, surface the breakage immediately and loudly. Returning degraded results hides the problem and trains users to trust unreliable output.

That argument is right for many systems. It is wrong for a long-running memory substrate that agents call on every turn. Consider: the cross-encoder reranker uses an ONNX model loaded into memory. If the model file is briefly unreadable during a config reload, fail-fast would error every retrieval until the reload completed. The agent turn fails. The user retries. The retrieval works. Nothing was learned; the user just lost a turn.

Memex chooses graceful degradation, with the trade-off paid in two places. First, every degraded fallback emits a circuit-breaker metric — the operator can dashboard the failure even though the user never saw it. Second, every code path that depends on a degradable component carries a documented fallback (raw cosine instead of cross-encoder reranker, identity tokenization instead of LLM-expanded query, default 0.5 instead of LLM-classified temporal proximity). You can read the fallback in code and reason about it.

The cost: more code paths. The benefit: agent turns survive transient LLM outages, and the operator still sees the breakage.

### Why P6 (append-only) and not "edit in place"

The pragmatic alternative would let you edit a memory unit's body — fix a typo, correct a misread date, refine the wording. Cheap, intuitive, exactly how a word processor works. The schema would be simpler. The retrieval path would be slightly faster (one row per claim instead of a chain).

That alternative loses lineage. Every claim a Memex agent surfaces carries an implicit promise: "this is what your January note said, here is the cite". If the January note's content is mutable, the cite is meaningless — by April the paragraph the cite points at might say something different. The agent told you the truth at the moment it spoke; the underlying evidence has since shifted underneath. There is no way to debug, no way to audit, no way to ask "what did the system believe in March".

Memex makes the opposite choice. Notes are immutable. A correction creates a new note with `superseded_by` pointing back. The old note stays — its content does not change. Retrieval-by-default finds the new one; retrieval-with-history finds both. The cite always points at content that existed at cite time.

The cost: more rows, more chain-walking when you want the latest. The benefit: every cite is a verifiable historical anchor, and the temporal queries that motivated Memex in the first place ("what did we decide last quarter") work as designed.

### Why P11 (local-first) and not cloud-first

A cloud-first memory service would be easier to ship. One backend, one auth model, one Postgres cluster you control. The team sets up the schema, pushes one binary, charges per query. Done.

Local-first costs more. The user has to run Postgres, manage their vault, decide whether to back it up. The team has to support many environments. Configuration becomes a thing.

Memex chooses local-first because the data is identity-shaped. Your memory of a project, your preferences, your decisions across years — handing those to a vendor is a bet that the vendor will still be in business in five years, will not change its terms, will not be acquired, will not de-prioritize your tier. Memex's bet is that the user owns their memory directly: Markdown files on disk, Postgres on their machine, a `git push` away from a self-hosted backup. The vendor disappears? Your vault is unchanged.

The cost: setup complexity. The benefit: no vendor lock-in. Ever.

## Implications for new features

When you design a feature that touches Memex, the principles tell you where to look — and what to ask.

Start with the invariants. If the feature wants to delete data: P1 says no, prefer downweight. Mark a row as deprioritized; do not unset it. Adding a hard-delete path is a human-gated GDPR special case, not a default.

If the feature wants to mutate a memory unit's body: P6 says no, version it. Create a new row with `superseded_by` pointing at the old id. The old row stays.

If the feature wants to add a global property to an entity: P7 says check whether the property is genuinely global. A canonical alias is global. A vault-specific outcome counter is not — that lives on `UnitEntity` or `MentalModel`.

If the feature wants to add a new LLM call: P9 says it must live behind a DSPy signature with Pydantic-typed I/O. No free-form prompt strings into the production path.

If the feature wants to add a new ranking signal: P3 (value, but with a structural straddler) says compose it at the existing log-additive chain, not as a parallel scoring path. The chain is bounded per-factor and bounded in aggregate; new factors fit the same shape.

If the feature touches a component that could fail: P12 says document the fallback. What does the calling code do when this component is unavailable? Write that down and wire it.

If the feature does anything: P13 says emit metrics. A new mechanism without a counter or a histogram is a knob without a gauge.

When something feels off in the codebase — when a new patch lands and you cannot tell whether it is right — the principles are also the right place to start a review. A patch that deletes data is the wrong shape (P1). A patch that mutates a memory unit is the wrong shape (P6). A patch that ranks results in a parallel path bypassing the composition chain is the wrong shape (P3 + the composition site straddler).

You will not need to look up the table every time. After a few features, the principles fade into instinct — the way a seasoned engineer instinctively reaches for an interface instead of a concrete class. The table is here for the moments when instinct is silent and you need to check.

### A worked example: adding a feedback signal

Imagine you are adding a feature that lets the user mark a memory unit with a one-to-five-star rating, and you want the rating to affect retrieval. Walk the principles.

P1 says the rating is non-destructive: a low rating downweights, it does not delete. Decision: store the rating as a column on `MemoryUnit`, do not let it gate visibility on its own.

P5 says you have to decide whether the rating is write-time or retrieval-time signal. The user gives the rating after reading the unit's content during an agent turn — that is post-write feedback, not classifier judgment. Decision: this signal accumulates lifelong, like outcome counters. Add a separate column (or aggregate on the existing outcome columns), do not stuff it into `importance`.

P6 says the rating event itself is auditable. Decision: write the rating as an event row in an audit table, with timestamp and user attribution, alongside whatever counter or aggregate the retrieval path reads.

P3 says the rating is a new ranking signal. Decision: it composes at the same log-additive chain as MW, FSFM decay, recency, temporal proximity, and confidence. Bound it, name an alpha tunable, emit a histogram.

P9 says if any LLM step is involved — say, you want an LLM to convert a free-text user comment into a rating — that LLM call lives behind a DSPy signature with a Pydantic-typed output.

P12 says if the LLM step is the only path to the rating, what happens when the LLM is down? Decision: fall back to a raw star input with no LLM enrichment.

P13 says you ship the histogram alongside the feature, not as a follow-up. The PR adds `RATING_BOOST_OBSERVED` to `metrics.py` in the same commit that adds the boost.

That walk took six principles and resolved most of the design before any code was written. The remaining decisions — schema names, alpha defaults, MCP tool shape — are mechanical compared to the choices the principles already settled.

## Back to the opening scene

The scenario this page opened with — the January Postgres note, the April SQLite note, the agent that returns the April fact without losing the January one — now reads as the natural output of six principles working together.

P6 makes both notes survive. The April note does not overwrite the January one; it creates a new row with `superseded_by` pointing at January.

P1 makes the January note remain searchable. It is downweighted, not deleted. `apply_pre_filter=False` surfaces it; default queries hide it.

P5 keeps the January note's original `importance` and `intent_class` intact. The classifier's January judgment is still on the row, available for debugging, untouched by the four months of activity since.

P3 lets the retrieval path see both notes through its five strategies and rank them at one composition chain. The April note ranks higher because of recency and FSFM decay; the January note is still in the candidate set.

P13 means the operator can dashboard exactly how often this kind of supersession happens — and whether the user reaches for the historical view often enough to justify exposing it more prominently in the agent surface.

P8 means the agent, not Memex, decides what to do with the conflicting evidence. Memex returns the April note with the January cite attached; the agent composes the answer.

Six principles, one user-visible behaviour. That is what consistency looks like when it is built into the foundation rather than patched onto the surface.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Deprioritize a memory unit](../how-to/deprioritize-units.md)
- [Reference: data model](../reference/data-model.md)
- [Explanation: architecture overview](how-memex-works/high-level-architecture.md)
