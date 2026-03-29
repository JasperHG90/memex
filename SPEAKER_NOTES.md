# Memex — Deep Dive Speaker Notes

Companion notes for `presentation-menu.html`. Each section maps to one menu item. Notes include analogies, code references, and talking points to make each topic accessible.

---

## 1. TEMPR: Five Ways to Remember

**The analogy:** Imagine you lost your car keys. You might retrace your steps (temporal), ask the person you were with (entity/graph), recall what you were thinking about (semantic), search for the word "keys" in your notes (keyword), or consult your mental model of "things I do when I get home" (mental model). TEMPR runs all five of these searches in parallel and merges the results.

### The five strategies

Each strategy is a class in `packages/core/src/memex_core/memory/retrieval/strategies.py`:

| Strategy | Class | What it does | Score type |
|----------|-------|-------------|------------|
| **Semantic** | `SemanticStrategy` (line 87) | pgvector cosine distance on embeddings | Lower = better |
| **Keyword** | `KeywordStrategy` (line 116) | PostgreSQL `ts_rank_cd` full-text search with BM25-style OR matching | Higher = better |
| **Graph** | `EntityCooccurrenceGraphStrategy` (line 381) | Two hops: direct entity links (1st order) + co-occurrence neighbors (2nd order) | Composite score |
| **Temporal** | `TemporalStrategy` (line 561) | Orders by `event_date` — newest first | Epoch timestamp |
| **Mental Model** | `MentalModelStrategy` (line 526) | Cosine distance on the separate `MentalModel` table — synthesized observations compete alongside raw facts | Lower = better |

### Reciprocal Rank Fusion (RRF)

**The analogy:** Five judges each rank contestants independently. RRF doesn't care about their raw scores (which are on different scales) — it only cares about the *rank*. A contestant ranked #1 by two judges and #5 by the others will beat one ranked #3 by everyone.

**The formula** (`engine.py:466-487`):
```
score(item) = sum( weight / (K + rank + 1) )    across all strategies
```

- `K = 60` — a smoothing constant. Higher K makes fusion more uniform; lower K gives more weight to top-ranked items.
- Each strategy contributes one rank per item. Items appearing in multiple strategies accumulate score.
- No score normalization needed — that's the beauty of rank-based fusion.

### Cross-encoder reranking

After RRF, the top candidates (capped at `min(limit * 2, 75)` to control cost) go through a neural cross-encoder (`engine.py:958-1027`):

1. **Cross-encoder scores** — ONNX model scores each (query, text) pair. Raw logits are normalized via sigmoid to [0, 1].
2. **Recency boost** — linear decay over 365 days: `recency = max(0.1, 1.0 - days_ago/365)`. A 6-month-old fact gets ~0.5, yesterday's gets ~1.0. Boost: `1.0 + 0.2 * (recency - 0.5)`, so the effective range is [0.92, 1.10].
3. **Temporal proximity boost** — same shape, driven by how close the fact's date is to dates mentioned in the query.
4. **Final score** = `cross_encoder * recency_boost * temporal_boost`.

**Key takeaway:** No single strategy wins. Temporal catches "last week" queries. Graph catches related entities. Semantic catches paraphrased concepts. Keyword catches exact terms. Fusion makes the whole greater than the parts.

---

## 2. Contradiction Detection

**The analogy:** Imagine a newsroom fact-checker. When a new article comes in, they don't check every sentence against the entire archive — that would take forever. Instead, they first skim for *corrective language* ("actually," "contrary to earlier reports," "has been updated to"). Only the flagged sentences get a deep check against similar existing stories.

### Two-stage pipeline

**File:** `packages/core/src/memex_core/memory/contradiction/engine.py`

**Stage 1 — Triage** (line 113): A single LLM call receives all new memory units and returns only the IDs that contain corrective language. Most units pass through untouched. This is the cheap filter.

```python
# DSPy signature (signatures.py:4-16):
# "Identify memory units that explicitly correct, update, revise, or supersede prior information.
#  Be conservative: only flag units with clear corrective language."
```

**Stage 2 — Classify** (line 208): For each flagged unit, the system retrieves candidates via entity overlap + semantic similarity (`candidates.py:16-43`), then asks the LLM to classify each pair as `reinforce`, `weaken`, or `contradict`. Neutral pairs are skipped entirely to save tokens.

### Confidence adjustment

Think of confidence as a health bar. Each relationship type adjusts it by `alpha = 0.1` (configurable):

| Relationship | Effect | Formula |
|-------------|--------|---------|
| **Reinforce** | Both units get healthier | `confidence = min(conf + 0.1, 1.0)` for *both* |
| **Weaken** | Superseded unit takes a hit | `confidence = max(conf - 0.1, 0.0)` for superseded only |
| **Contradict** | Superseded unit takes a double hit | `confidence = max(conf - 0.2, 0.0)` for superseded only |

When confidence drops below `0.3` (the `superseded_threshold`), the unit is excluded from default retrieval — but never deleted. The full history is always preserved.

### Authority resolution (line 244)

**The analogy:** In a courtroom, the most recent testimony usually wins — unless the earlier witness has stronger evidence. Same here:

1. **Default:** newer event date wins (temporal heuristic).
2. **Override:** the LLM can flag that the *older* unit is actually more authoritative (e.g., "the original specification states...").
3. Every decision is recorded in a `MemoryLink` with full provenance: which unit won, why, and which note triggered it. This creates a lineage graph of how facts evolve over time.

---

## 3. Semantic Tree Chunking

**The analogy:** When you get a 50-page PDF, you don't read it cover to cover to find one section. You look at the table of contents, pick the relevant chapter, and read just that. PageIndex builds that table of contents automatically.

### How it works

**File:** `packages/core/src/memex_core/memory/extraction/core.py`

**Step 1 — Detect structure** (line 889): The system scans for Markdown headers with regex (`## Introduction`, `### Methods`, etc.) and assesses quality:

```python
# utils.py — assess_structure_quality() checks:
# - At least 2 headers (MIN_HEADERS_FOR_STRUCTURED)
# - Headers cover >= 50% of content (MIN_COVERAGE_FOR_STRUCTURED)
# - No gap exceeds 40% of the document (MAX_GAP_RATIO_FOR_STRUCTURED)
# - Average section <= 4000 tokens (MAX_AVG_SECTION_TOKENS)
```

**Step 2 — Build the tree:**

- **Fast path** (line 913): If the document is well-structured Markdown, build the tree directly from regex headers using a stack-based algorithm (`utils.py:250-283`). Zero LLM calls — it's pure string parsing. Headers become tree nodes, nesting follows header levels (h1 > h2 > h3).

- **LLM path** (line 956): For messy documents (PDFs, pasted text, no headers), the LLM scans chunks in parallel (`_scan_document_parallel`, line 1040) to detect logical sections. Uses fuzzy matching (15% tolerance) to anchor detected headers to exact positions in the text. If LLM accuracy is below 60%, falls back to merging with whatever regex headers were found.

**Step 3 — Hydrate content** (`utils.py:328`): Each tree node gets its text slice and a token count via `tiktoken` (`cl100k_base` encoding).

**Step 4 — Generate blocks** (`utils.py:474`): Adjacent nodes are merged into blocks up to a target token size. Each block gets an MD5 content hash for incremental diffing.

### The retrieval flow

This is where it gets surgical:

1. **`get_page_indices`** — returns the thin tree (titles, token counts, node IDs). An LLM can look at this and say "I need section 3.2 and 5.1."
2. **`get_nodes`** — fetches only the text for those specific node IDs.

Instead of dumping 50 pages into context, you might read 2 pages. That's the token savings.

---

## 4. Incremental Re-ingestion

**The analogy:** When you edit a Google Doc, Google doesn't re-index every word — it diffs the change and updates only what moved. Memex does the same with its knowledge extraction.

### Hash-based diffing

**File:** `packages/core/src/memex_core/memory/extraction/core.py` (line 186)

Every block gets a SHA-256 hash of its whitespace-normalized text:
```python
def content_hash(text: str) -> str:
    normalized = re.sub(r'[ \t]+', ' ', text.strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
```

When a note is updated, old hashes are compared against new hashes.

### Four categories

**File:** `packages/core/src/memex_core/memory/extraction/pipeline/diffing.py`

| Category | What happened | Action |
|----------|--------------|--------|
| **Retained** | Hash exists in both old and new | Skip entirely — no LLM calls |
| **Content changed** | New hash, contains new/modified nodes | Extract facts (LLM call) |
| **Boundary shift** | New hash, but all constituent nodes existed before (just moved between blocks) | Migrate existing facts to new block IDs — no LLM calls |
| **Removed** | Hash exists only in old | Mark facts as stale |

### Boundary shift — the clever bit

**The analogy:** Imagine a book where chapters are renumbered but the text is identical. You don't re-read the book — you just update the chapter references.

When block boundaries shift (e.g., a paragraph moved from block 3 to block 4), the system finds the new block with the most node overlap and reassigns facts via a single SQL `CASE` statement (`storage.py:669-691`):

```python
stmt = update(MemoryUnit) \
    .where(col(MemoryUnit.chunk_id).in_(old_ids)) \
    .values(chunk_id=case(*whens))  # Batch reassignment
```

### Context for new blocks

When extracting facts from newly added blocks, the system includes the adjacent retained blocks as read-only context (`diffing.py:383-428`). This gives the LLM document continuity without re-extracting those neighbors. Think of it as showing the LLM the paragraph before and after the new one, so it understands the context.

---

## 5. Hybrid MMR Diversity

**The analogy:** You're building a playlist. You have 20 great rock songs, but playing them all in a row gets repetitive. MMR says: pick the best song first, then for each next pick, balance "how good is it?" against "how different is it from what's already on the list?"

### The formula

**File:** `packages/core/src/memex_core/memory/retrieval/engine.py` (line 1101)

```
MMR(candidate) = lambda * relevance(candidate) - (1 - lambda) * max_similarity(candidate, selected)
```

- `lambda = 0.9` (default, conservative) — heavily favors relevance, gently penalizes redundancy.
- `relevance` = normalized position rank from the reranker output: `(n - i) / n`.
- `max_similarity` = the highest similarity between the candidate and any already-selected item.

### Hybrid similarity

Similarity isn't just embeddings — it's a blend (line 1085):

```
similarity = 0.6 * cosine_embedding + 0.4 * entity_jaccard
```

- **Cosine** (60%) catches semantic overlap ("the auth system was rewritten" vs "auth middleware was refactored").
- **Entity Jaccard** (40%) catches cases where embeddings differ but the facts are about the same entities. Jaccard = `|shared entities| / |all entities|`.

### The greedy algorithm

1. Always pick the top-ranked result first.
2. For each remaining slot: score every candidate with the MMR formula, pick the highest.
3. Tiebreaker (within `epsilon = 0.01`): prefer newer `event_date`.
4. Virtual/synthesized units (mental models injected into results) are excluded from diversity computation and re-inserted at their original positions afterward.

---

## 6. Reflection Engine

**The analogy:** Reflection is like a researcher who reads your raw field notes at the end of each week and writes a summary paper. The field notes (memory units) are the evidence; the summary paper (mental model) is the synthesized understanding. Over time, the papers get revised as new evidence comes in.

### Six-phase cycle

**File:** `packages/core/src/memex_core/memory/reflect/reflection.py` (line 217)

| Phase | Name | What happens | LLM? |
|-------|------|-------------|------|
| 0 | **Update** (line 557) | Check existing observations against new evidence. Prune stale evidence. | Yes |
| 1 | **Seed** (line 656) | Generate candidate observations from recent memories. "What patterns do you see?" | Yes |
| 2 | **Hunt** (line 706) | Vector search for supporting/contradicting evidence for each candidate. | No (DB only) |
| 3 | **Validate** (line 800) | Extract exact quotes from evidence. Reject hallucinated observations that lack support. | Yes |
| 4 | **Compare** (line 857) | Merge new observations with existing ones. Compute trends. Generate entity summary. | Yes |
| 5 | **Finalize** (line 292) | Compute embeddings, persist the updated mental model. | No |
| 6 | **Enrich** (line 328) | Push new tags back into contributing memory units (Memory Evolution). | Yes |

### Trend labels

**File:** `packages/core/src/memex_core/memory/reflect/trends.py`

After Phase 4, each observation gets a trend computed from evidence density:

```python
recent_density = len(recent_evidence) / 30   # evidence per day, last 30 days
older_density = len(older_evidence) / 60      # evidence per day, 30-90 days ago

ratio = recent_density / older_density
```

| Ratio | Trend | Meaning |
|-------|-------|---------|
| No recent evidence | **Stale** | Nobody's mentioned this lately |
| All evidence is recent | **New** | Just discovered |
| ratio > 1.5 | **Strengthening** | More evidence piling up |
| ratio < 0.5 | **Weakening** | Mentions are fading |
| else | **Stable** | Steady state |

### Distributed queue

**File:** `packages/core/src/memex_core/memory/reflect/queue_service.py` (line 194)

Reflection uses PostgreSQL's `SELECT ... FOR UPDATE SKIP LOCKED` for atomic task claiming. Multiple workers can poll the queue concurrently — each grabs different tasks, no conflicts. Priority is calculated from three signals:

```
Priority = (weight_urgency * evidence_count)
         + (weight_importance * log10(mention_count))
         + (weight_resonance * log10(retrieval_count))
```

Entities that are frequently mentioned, frequently retrieved, and have lots of new evidence get reflected on first.

### Concurrency

`reflect_batch()` (line 113) processes entities in parallel via `asyncio.gather`, controlled by a semaphore (`max_concurrency`). Five entities take the same wall-clock time as one. Each entity also acquires a PostgreSQL advisory lock (`pg_try_advisory_xact_lock`) to prevent two workers from reflecting on the same entity simultaneously.

---

## 7. Entity Disambiguation

**The analogy:** You're at a party and someone mentions "Alex." Is this your coworker Alex, your cousin Alex, or Alex the barista? You use three clues: how close the name sounds (maybe they said "Alec"), who else they were talking about (if they mentioned your office, probably your coworker), and when they last saw this Alex (if they said "yesterday" and you saw your coworker yesterday...).

### The scoring formula

**File:** `packages/core/src/memex_core/memory/entity_resolver.py` (line 53)

```
score = 0.5 * name_similarity + 0.3 * co_occurrence + 0.2 * temporal_proximity
```

**Threshold: 0.65** — score must reach this to match. Below that, a new entity is created.

### Name similarity (50%)

PostgreSQL trigram similarity (`pg_trgm`) with a phonetic floor:

```sql
-- Trigram: "Alexander" vs "Aleksander" → ~0.6
similarity(lower(canonical_name), lower(input_text))

-- Also checks aliases: "Alex" might be an alias of "Alexander"
-- Also checks phonetic codes via Double Metaphone
```

If the metaphone codes match but trigrams are low (e.g., "Geoff" vs "Jeff" — trigram ~0.3, but same phonetic code), the score floors at 0.5.

### Co-occurrence (30%)

**The analogy:** If "Alex" always appears alongside "the Kubernetes cluster" and "the SRE team," and your current document also mentions those, it's probably the same Alex.

Uses TF-IDF-style weighting:
```python
weight = 1.0 / log2(2 + mention_frequency)
```

Rare shared neighbors (mentioned only a few times) are exponentially stronger signals than common ones. If "Alex" and "the Kubernetes cluster" have only co-occurred twice, that's a strong signal. If "Alex" and "the" have co-occurred 10,000 times, that's meaningless.

### Temporal proximity (20%)

Exponential half-life decay with a 30-day default (`utils.py:80`):

```python
score = 2 ** (-days_since_last_seen / 30)
```

| Days ago | Score |
|----------|-------|
| 0 | 1.0 |
| 30 | 0.5 |
| 60 | 0.25 |
| 90 | 0.125 |

An entity seen yesterday is much more likely to be the same one mentioned today than one seen 3 months ago.

### Intra-batch dedup

Before candidate lookup, entities within the same document are grouped by normalized name (`_prepare_inputs`, line 137). If "Alex" appears 5 times in one document, it's resolved once, and all 5 indices point to the same result.

---

## 8. The Rust Migration: 206 Tickets in a Day

This is a war story, not a code walkthrough. Key talking points:

- **Setup:** Attempted a full port of the Python codebase to Rust using parallel Claude Code worktree agents — each agent worked in its own isolated git worktree.
- **Scale:** 206 tickets completed (183 parity tasks + 23 sweep/cleanup tasks). 484 tests total: 448 unit, 28 integration, 8 LLM integration.
- **What worked:** Mechanical translation — type definitions, data models, CRUD operations, SQL queries. Worktree isolation meant agents couldn't step on each other.
- **What broke:** The async Python → Rust translation for DSPy/LLM integration was too dynamic. Python's duck typing and runtime introspection don't map cleanly to Rust's static type system. The extraction pipeline's heavy use of `dspy.Predict` with runtime-defined signatures was the breaking point.
- **Lesson:** Parallel worktree agents are powerful for mechanical tasks but struggle with architectural decisions that require holistic understanding of the system. The agents couldn't see the forest for the trees.

---

## 9. Lineage: Following the Paper Trail

**The analogy:** Academic citations work in two directions. You can follow a paper's references *upstream* to find its sources, or search *downstream* to find papers that cite it. Lineage does both for Memex knowledge.

### The chain

```
Document (Note)
  └── Memory Unit (extracted fact)
        └── Observation (validated insight)
              └── Mental Model (synthesized understanding per entity)
```

### Bidirectional traversal

**File:** `packages/core/src/memex_core/services/lineage.py`

- **Upstream** (`_get_lineage_upstream`, line 269): "Why does Memex believe X?" Start at a mental model → walk through its observations → find the memory units cited as evidence → arrive at the original document and paragraph.
- **Downstream** (`_get_lineage_downstream`, line 109): "What knowledge was derived from this document?" Start at a note → find all extracted facts → find observations that cite those facts → find mental models built from those observations.

### JSONB path queries

Observations are stored as JSONB inside mental models. To find which mental models reference a specific memory unit, the system uses PostgreSQL JSONB path queries:

```sql
-- "Find mental models where any observation cites memory unit X"
jsonb_path_exists(
    observations,
    '$[*].evidence[*].memory_id ? (@ == "unit-uuid-here")'
)
```

This is much faster than deserializing every mental model's observations in Python.

### Depth control

Traversal depth is configurable (default: 3, max: 10). At each level, children are capped by a `limit` parameter (default: 10). This prevents runaway traversals on densely connected graphs — imagine an entity like "Kubernetes" that appears in hundreds of documents.

### Multi-vault

The same entity can have different mental models in different vaults. When traversing lineage for an entity with multiple mental models, the system wraps them under an `entity` node, with each vault's mental model as a separate branch.

### Exposure

- **REST API:** `GET /api/v1/lineage/{entity_type}/{id}?direction=upstream&depth=3`
- **MCP:** `memex_get_lineage` tool — accepts `entity_type`, `entity_id`, `direction` (upstream/downstream/both), `depth`, and `limit` parameters.

---

## 10. Memory Evolution (Phase 6 Enrichment)

**The analogy:** Imagine you filed a document under "infrastructure" three months ago. This week, your team realizes the infrastructure rewrite was actually driven by a compliance requirement. Memory Evolution goes back and adds "compliance" tags to that old document — making it discoverable for a concept you didn't even know about when you filed it.

### The problem

After reflection builds a mental model, that understanding stays trapped at the model layer. A 3-month-old memory about "auth middleware rewrite" (tags: `auth, middleware`) is invisible to "compliance work" queries, even though reflection now knows the rewrite is compliance-driven.

### How Phase 6 works

**File:** `packages/core/src/memex_core/memory/reflect/reflection.py` (line 328)

1. Collect evidence unit IDs from the observations that were just finalized.
2. Load those contributing memory units from the database.
3. LLM generates enriched tags based on the mental model context (using `EnrichmentSignature` from `prompts.py:235`).
4. Write `enriched_tags`, `enriched_keywords`, `enriched_at`, and `enriched_by_entity` into `unit_metadata`.

### Safety guarantees

- **Append-only:** All enrichment keys are prefixed with `enriched_` — original metadata is never touched.
- **Accumulative:** Tags are set-unioned across reflection cycles (`existing_tags | new_tags`), never overwritten.
- **Auditable:** `enriched_at` timestamp and `enriched_by_entity` name enable tracing.
- **Configurable:** Controlled by `enrichment_enabled: true` in reflection config. Can be disabled without affecting existing enriched metadata.

### Impact on retrieval

The Keyword strategy's tsvector now includes `enriched_tags` and `enriched_keywords` via `COALESCE` on the JSONB metadata. So the "auth middleware rewrite" memory now matches keyword searches for "compliance" — without the original extraction having known about compliance at all.

---

## 11. Multi-Vault Knowledge Isolation

**The analogy:** Vaults are like separate filing cabinets with locks. Your personal cabinet and your work cabinet are completely separate. But you might give a colleague a *read-only* key to your reference cabinet.

### How it works

**File:** `packages/core/src/memex_core/server/auth.py`

Every query is filtered through `apply_vault_filters()` (`strategies.py:39`):
```python
def apply_vault_filters(statement, vault_id_col, **kwargs):
    vault_ids = kwargs.get('vault_ids')
    if not vault_ids:
        return statement  # Superuser: no filter
    return statement.where(col(vault_id_col).in_(vault_ids))
```

All five TEMPR strategies, entity resolution, contradiction detection, and reflection respect these boundaries.

### Policy-based ACL

Three roles with escalating permissions:

| Role | Read | Write | Delete |
|------|------|-------|--------|
| **Reader** | Yes | No | No |
| **Writer** | Yes | Yes | No |
| **Admin** | Yes | Yes | Yes |

Each API key is scoped to specific vaults with a policy. The `AuthContext` (line 34) carries the resolved permissions through every request.

### Cross-vault read access

`read_vault_ids` grants read-only access to additional vaults:
```
Effective read scope  = vault_ids + read_vault_ids
Effective write scope = vault_ids only
```

**Use case:** A consulting team gets write access to their project vault and read access to a shared reference vault. They can read company-wide knowledge but only write to their own space.

---

## 12. From Paper to Production: Hindsight 20/20

**The analogy:** Academic papers are blueprints. Production systems are buildings. The blueprint says "load-bearing wall here" — the builder has to figure out what concrete to use, how deep the foundation needs to be, and whether the building code allows it.

### What survived from the paper

- **TEMPR** — the five-strategy parallel retrieval with RRF fusion. This is the core insight: no single retrieval method wins for all query types.
- **Reflection loop** — synthesize higher-order observations from raw facts. The paper's concept of "mental models" maps directly.
- **Entity-centric memory graph** — entities as the connective tissue between facts.

### What changed

The paper's four memory networks (World, Experience, Opinion, Observation) became three fact types in `memex_common/types.py`:

| Paper | Production | Why |
|-------|-----------|-----|
| World | `WORLD` | Static knowledge, system states — kept as-is |
| Experience | `EVENT` | Renamed for clarity — episodic, narrative occurrences |
| Opinion | *(removed)* | Reflection is objective, not persona-driven |
| Observation | `OBSERVATION` | Synthesized mental models — promoted to first-class type |

### What got thrown out

- **Personality profiles and disposition parameters** — the paper had agents with personalities. Production reflection is objective.
- **Scalar confidence system** — replaced with linear confidence adjustments via contradiction detection (`alpha = 0.1` steps).
- **CARA reasoning agent loop** — replaced with direct DSPy predictor calls for ~30% less token generation.
- **Synchronous Python** — replaced with async everything (AsyncIO + Pydantic + SQLModel on PostgreSQL + pgvector).

### The key lesson

Academic architectures give you the "what" but production forces you to answer "how fast?" and "at what cost?" Every LLM call had to justify its token budget.

---

## 13. Multi-Query Expansion

**The analogy:** When you Google something and get bad results, you instinctively rephrase the query. Multi-query expansion does this automatically — the LLM generates 1-2 rephrased versions of your query, and all versions run through the full retrieval pipeline.

### How it works

**File:** `packages/core/src/memex_core/memory/retrieval/expansion.py`

```python
class QueryExpansionSignature(dspy.Signature):
    query: str = dspy.InputField(desc='The original search query.')
    variations: list[str] = dspy.OutputField(
        desc='A list of 1-2 semantic variations or expanded versions of the query.'
    )
```

### Weighted fusion

**File:** `packages/core/src/memex_core/memory/retrieval/document_search.py` (line 114)

Each query variant runs all 5 TEMPR strategies independently. Results are fused via RRF with weights:

| Query | Weight |
|-------|--------|
| Original | **2.0** |
| Expansion 1 | 1.0 |
| Expansion 2 | 1.0 |

The 2x weight on the original ensures expansions supplement but don't override user intent. If the original query already finds great results, expansions can only help, not hurt.

**Controlled by:** `expand_query=True` on search calls. Off by default — trades latency for recall on hard queries.

---

## 14. LoCoMo: Evaluating Memory

**The analogy:** LoCoMo is like a standardized test for memory systems. It gives you a long conversation, then asks questions that require remembering specific facts, connecting multiple facts, reasoning about time, and handling trick questions.

### The benchmark

**File:** `packages/eval/src/memex_eval/external/`

Five question categories:

| Category | What it tests |
|----------|--------------|
| **Single-hop** | Direct fact recall ("What did X say about Y?") |
| **Multi-hop** | Connecting facts across conversation turns |
| **Temporal** | Time-based reasoning ("What happened after X?") |
| **Open-domain** | General knowledge that should NOT be in memory |
| **Adversarial** | Subject-swapped questions designed to trick the system |

### Results

- **0.986 accuracy** on non-adversarial questions (35/36 perfect).
- **$9.96 total** for 50 questions (~$0.20/query). Retrieval tokens are only 4.5% of total — the rest is LLM reasoning.
- Graded scoring: `[0.0, 0.25, 0.5, 0.75, 1.0]` — not just pass/fail.

### Tool-choice analysis

The evaluator tracks *how* the agent answers — which MCP tools it calls and in what order:
- **Simple two-stage** (38%): `memory_search` → answer
- **Deep verification** (30%): `memory_search` → `note_search` → `get_page_index` → `get_nodes` → answer
- **Entity exploration** (12%): `entity_search` → `memory_search` → answer

Adversarial failures are by design — the benchmark swaps subjects ("What did Bob say?" when it was actually Alice), and a correct search tool rightly returns Bob's actual statements, not Alice's.

---

## 15. Atomic Extraction Pipeline

**The analogy:** Bank transfers must be atomic — if the debit succeeds but the credit fails, the money vanishes. Memex extraction works the same way: either the entire pipeline (parse → chunk → extract → embed → store) succeeds, or nothing changes.

### Two-phase commit

**File:** `packages/core/src/memex_core/storage/filestore.py` (line 110)

1. **Stage:** Files are written with a `.stage_{txn_id}` suffix.
2. **Commit:** Database transaction commits. If this succeeds, staged files are renamed to final paths via `asyncio.gather`.
3. **Rollback:** If any stage fails, staged files are deleted, DB transaction rolls back.

```python
target = f'{key}.stage_{txn_id}'   # Temporary path during staging
stage.staged_files[target] = key    # Maps to final path on commit
```

### Hash-based idempotency

Two hash algorithms serve different purposes:

| Algorithm | Used for | Why |
|-----------|---------|-----|
| **MD5** | Node-level identity (`content_hash_md5`) | Fast, deterministic IDs for tree nodes. Re-ingesting identical content is a no-op. |
| **SHA-256** | Block-level content diffing (`content_hash`) | Cryptographic strength for detecting any content change, even adversarial. Used by incremental re-ingestion. |

### The guarantee

Zero partial state: you never end up with facts in the database pointing to a note that doesn't exist on disk, or vice versa. If the extraction LLM fails halfway through, the filesystem and database both look exactly as they did before the attempt.

---

## 16. Pluggable Note Templates

**The analogy:** Templates are like cookie cutters. The system ships with a few standard shapes (General Note, ADR, RFC), you can add your own globally or per-project, and project-local ones override global ones with the same name.

### Three-layer discovery

**File:** `packages/common/src/memex_common/templates.py`

| Layer | Location | Scope |
|-------|----------|-------|
| **Builtin** | `memex_common/prompts/*.toml` | Ships with the package. 5 templates: General Note, Technical Brief, ADR, RFC, Quick Note. Protected from deletion. |
| **Global** | `{filestore_root}/templates/*.toml` | User-level. Shared across all instances. |
| **Local** | `.memex/templates/*.toml` | Project-scoped. Override global or builtin for a specific repo. |

Later layers override earlier ones on slug collision. So if you create a `general-note.toml` in your project's `.memex/templates/`, it replaces the builtin one — for that project only.

### TOML format

```toml
name = "Architectural Decision Record"
description = "Document an architectural decision with context and consequences"

template = """---
title: "ADR: {title}"
date: {date}
author: {author}
tags: [architecture, decision]
---

## Context
...
"""
```

### Registry

`TemplateRegistry` (line 84) manages discovery, registration, and deletion. It scans all three layers, deduplicates by slug (last writer wins), and exposes `list()` and `get(slug)` methods used by both CLI (`memex note template --list`) and MCP.

---

## 17. Pluggable Inference Backends

**The analogy:** Memex's embedding and reranking models are like interchangeable camera lenses. The camera body (retrieval engine) doesn't care which lens is attached — it just needs an image. You can use the kit lens (built-in ONNX) or swap in a premium lens (OpenAI, Cohere, Ollama via LiteLLM).

### Protocol-based design

**File:** `packages/core/src/memex_core/memory/models/protocols.py`

```python
class EmbeddingsModel(Protocol):
    def encode(self, text: list[str]) -> Any: ...

class RerankerModel(Protocol):
    def score(self, query: str, texts: list[str]) -> Any: ...
```

Any backend just needs to implement these two methods. The retrieval engine never knows which backend is running.

### Two backend types

| Backend | Config | What it does |
|---------|--------|-------------|
| **ONNX** (default) | `type: onnx` | `FastEmbedder` / `FastReranker` — runs locally, no network calls. Fine-tuned ms-marco-MiniLM for reranking. |
| **LiteLLM** | `type: litellm` | Proxies to any LiteLLM-supported provider: OpenAI, Gemini, Cohere, Ollama, etc. |

Factory functions (`get_embedding_model()`, `get_reranking_model()`) dispatch on the config `type` field.

### The logit transform

The retrieval engine applies sigmoid normalization to reranker scores — it was built for ONNX, which outputs raw logits (unbounded numbers). LiteLLM providers return [0, 1] scores directly. Feeding [0, 1] into sigmoid again would compress everything toward 0.5.

The LiteLLM adapter applies **inverse sigmoid (logit)** first:
```python
clamped = max(1e-7, min(1 - 1e-7, score))
logit = math.log(clamped / (1 - clamped))
```

Then the engine's sigmoid recovers the original score. This keeps the engine unchanged — backends are responsible for speaking the same "language" (raw logits).

### Batched ONNX inference

The ONNX reranker batches query-document pairs to control memory usage (configurable via `reranker_batch_size` in config, default 0 = all at once). This prevents OOM on large result sets, especially on GPU-constrained devices like Jetson.

---

## 18. OpenTelemetry Observability

**The analogy:** OpenTelemetry is like flight recorder data for your system. Every operation — LLM call, database query, retrieval strategy — gets a timestamped trace. When something goes wrong (bad retrieval results, slow response), you can replay the entire sequence in Arize Phoenix and see exactly what happened.

### Setup

**File:** `packages/core/src/memex_core/tracing.py`

```python
def setup_tracing(config: TracingConfig) -> None:
    exporter = OTLPSpanExporter(endpoint=config.endpoint, headers=config.headers)
    provider = TracerProvider(resource=Resource.create({'service.name': config.service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    LiteLLMInstrumentor().instrument(tracer_provider=provider)
```

Optional dependency — install with `uv add memex-core[tracing]`.

### Session ID propagation

**File:** `packages/core/src/memex_core/server/__init__.py` (line 210)

HTTP middleware reads `X-Session-ID` from the request header (or generates one). This ID is bound to:
1. Python's `ContextVar` for structlog
2. OpenInference's `using_session()` context manager for span grouping

In Arize Phoenix, you can filter all traces from a single user session — every retrieval, extraction, and reflection trace grouped together.

### DSPy operation names

**File:** `packages/core/src/memex_core/llm.py` (line 51)

Every `dspy.Predict` call gets a named span:

```python
with tracer.start_as_current_span(operation_name, kind=SpanKind.INTERNAL):
    # e.g., 'extraction.facts', 'reflection.seed', 'contradiction.triage'
```

This means in Phoenix you can see: "This retrieval took 2.3s — 0.1s was embedding, 0.8s was the five TEMPR strategies, 1.2s was the reranker, and 0.2s was MMR diversity filtering."

### Background job tracking

**File:** `packages/core/src/memex_core/context.py` (line 31)

Background reflection jobs get their own session IDs via `background_session('bg-reflect')`:
```python
async with background_session('bg-reflect'):
    # All spans within this block carry session_id='bg-reflect-a1b2c3d4e5f6'
```

No more orphaned spans — periodic reflection tasks triggered by the scheduler are fully traceable.

### Span kinds

All internal operations use `SpanKind.INTERNAL` to avoid confusing Phoenix's trace visualizer. Without this, LiteLLM's auto-instrumented spans (which use `SpanKind.CLIENT`) would create phantom server/client relationships in the trace graph.
