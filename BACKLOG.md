# Backlog: Pluggable Graph Retrieval + Memory Link Strategies

## Execution Order

```
T1 (Pluggable Infrastructure) → T2 (Causal) ─┐
                                → T3 (Semantic Seeding) ─┤
                                → T4 (LinkExpansion) ─────┤
T5 (DateParser) ──────────────────────────────────────────┤ [independent]
T6 (Cross-Encoder Boosts) ────────────────────────────────┤ [independent]
T7 (Fact-Type RRF) ───────────────────────────────────────┘ [independent]
```

T1 is the only blocker. T2/T3/T4 depend on T1. T5/T6/T7 are fully independent.

---

## T1: Pluggable Graph Retriever — Size M

**Goal**: Make graph retrieval strategy selectable via config without changing default behavior.

**Files**:
- `packages/core/src/memex_core/memory/retrieval/strategies.py` — Rename + extract + factory
- `packages/common/src/memex_common/config.py` — New config field
- `packages/core/src/memex_core/memory/retrieval/engine.py` (~line 149) — Use factory
- `packages/core/src/memex_core/memory/retrieval/document_search.py` (~line 95) — Accept config, use factory
- `packages/core/src/memex_core/api.py` — Pass config through

**Sub-tasks**:
1. Rename `GraphStrategy` → `EntityCooccurrenceGraphStrategy` (keep alias)
2. Rename `NoteGraphStrategy` → `EntityCooccurrenceNoteGraphStrategy` (keep alias)
3. Extract `build_seed_entity_cte(query, ner_model, similarity_threshold, ...)` helper from lines 176-251. **Note**: `vault_id` is NOT used in seed entity building — vault filtering is applied downstream via `apply_vault_filters` on first/second order queries. Also note that `NoteGraphStrategy._build_seed_entities()` (line 413) has additional `ilike` fuzzy matches per entity name in the NER path that `GraphStrategy` does not — the unified helper must reconcile this divergence (recommend including the `ilike` path with an opt-in flag).
4. Add `graph_retriever_type: str = 'entity_cooccurrence'` to `RetrievalConfig`
5. Create `get_graph_strategy(type, ner_model, **kwargs)` factory
6. Create `get_note_graph_strategy(type, ner_model, **kwargs)` factory
7. Wire factory into `RetrievalEngine.__init__()` replacing hardcoded constructor
8. Wire factory into `NoteSearchEngine.__init__()`, accept `retrieval_config` param
9. Pass `retrieval_config` through `MemexAPI.__init__()` → `NoteSearchEngine`
10. Tests: default behavior unchanged, factory returns correct types, unknown type → ValueError

**AC**: Default config identical to current behavior. `build_seed_entity_cte()` reusable. `just prek` passes.

---

## T2: Causal Expansion Strategy — Size M *(depends: T1)*

**Goal**: Add `CausalGraphStrategy` expanding from NER seeds through `memory_links` causal edges.

**Files**:
- `strategies.py` — New `CausalGraphStrategy` + `CausalNoteGraphStrategy`
- `config.py` — `causal_weight_threshold: float = 0.3`

**SQL CTE design**:
```
seed_entities    → build_seed_entity_cte() [from T1]
first_order      → seed → UnitEntity → MemoryUnit (score = 1.0 + temporal_decay)
causal_expansion → first_order.id → memory_links WHERE link_type IN
                   ('causes','caused_by','enables','prevents') AND weight >= threshold
                   → to_unit_id → MemoryUnit (score = link_weight × 0.8)
combined         → UNION ALL + GROUP BY id, MAX(score)
```

**Sub-tasks**:
1. `CausalGraphStrategy` with seed + 1st order CTEs (reuse `build_seed_entity_cte`). **Note**: `MemoryLink` is not currently imported in `strategies.py` — add import.
2. Causal expansion CTE joining `MemoryLink` (use `idx_memory_links_from_weight` index, which has partial filter `weight >= 0.1`)
3. Score combination via UNION ALL + GROUP BY MAX
4. `CausalNoteGraphStrategy` mirror for Chunk-based search
5. `causal_weight_threshold` config field (default 0.3)
6. Register `'causal'` in both factory functions
7. Unit tests: SQL compilation, threshold filtering, score ordering
8. Integration test: retain causal facts → query → verify causal neighbors returned

**AC**: `graph_retriever_type='causal'` activates. 2nd order scores < 1st order. No N+1 queries. `just prek` passes.

---

## T3: Semantic-Seeded Graph Discovery — Size S *(depends: T1)*

**Goal**: Augment NER-based seed entity discovery with semantic seeds from `query_embedding`.

**Rationale**: Current graph entry requires entities explicitly named in query. Implicit queries ("Who recommended the national park?") fail because NER won't extract "Yosemite". Semantic seeding finds top-K similar MemoryUnits, reverse-traverses `UnitEntity` to discover their entities.

**Files**:
- `strategies.py` — Modify `build_seed_entity_cte()` or add `build_semantic_seed_cte()`
- `config.py` — 3 fields: `graph_semantic_seeding: bool = True`, `graph_semantic_seed_top_k: int = 5`, `graph_semantic_seed_weight: float = 0.7`

**SQL design**:
```
ner_seeds      → current NER path (weight = 1.0)
semantic_seeds → MemoryUnit WHERE status = 'active'
                 ORDER BY embedding <=> query_embedding LIMIT top_k
               → JOIN UnitEntity → SELECT DISTINCT entity_id (weight = 0.7)
               (NOTE: WHERE status = 'active' required to use idx_memory_units_embedding_active HNSW index)
seed_entities  → UNION ALL ner_seeds + semantic_seeds
               → GROUP BY id, MAX(weight) [NER wins on overlap]
```

**Sub-tasks**:
1. Add `build_semantic_seed_cte(query_embedding, vault_id, top_k, weight)` helper
2. Modify `build_seed_entity_cte()` to accept optional `query_embedding` and union semantic seeds
3. Add 3 config fields to `RetrievalConfig`
4. Pass config through to strategy constructors
5. Update 1st-order score to use `seed_entities.c.weight` instead of `literal(1.0)` (line 286)
6. Unit tests: SQL with/without embedding, semantic_seeding=False skips it
7. Integration test: implicit entity query returns results via semantic path

**AC**: With embedding → UNION seed CTEs. Without → identical to current. NER weight (1.0) > semantic weight (0.7). Uses existing HNSW index `idx_memory_units_embedding_active`. `just prek` passes.

---

## T4: Link Expansion Strategy — Size M *(depends: T1)*

**Goal**: Port Hindsight's LinkExpansion as a new graph strategy — single CTE expanding through 3 signals (entity co-occurrence, semantic kNN, causal chains) with additive scoring.

**Reference**: `.temp/hindsight/hindsight-api/hindsight_api/engine/search/link_expansion_retrieval.py`

**Files**:
- `strategies.py` — New `LinkExpansionGraphStrategy` + `LinkExpansionNoteGraphStrategy`
- `config.py` — `link_expansion_causal_threshold: float = 0.3`

**SQL CTE design** (3 parallel expansion CTEs from 1st-order seed units):
```
entity_expanded  → seed units → memory_links(type='entity') → target units
                   score = tanh(COUNT(DISTINCT entity_id) × 0.5)
semantic_expanded → seed units → memory_links(type='semantic') → target units
                   UNION seed units ← memory_links(type='semantic', to_unit_id=seed)
                   score = MAX(weight) [bidirectional kNN]
causal_expanded  → seed units → memory_links(type IN causal_types) → target units
                   score = weight, WHERE weight >= threshold
combined         → UNION ALL all 3 → GROUP BY id
                   final_score = SUM(entity_score + semantic_score + causal_score) [additive, range 0-3]
```

**Sub-tasks**:
1. `LinkExpansionGraphStrategy` class with `build_seed_entity_cte()` + 1st order
2. Entity expansion CTE: `memory_links(type='entity')` → `COUNT(DISTINCT entity_id)` → `tanh(count × 0.5)`. **Note**: `MemoryLink.entity_id` is nullable — filter `WHERE entity_id IS NOT NULL` to avoid incorrect counts.
3. Semantic expansion CTE: bidirectional join on `memory_links(type='semantic')` → `MAX(weight)`
4. Causal expansion CTE: `memory_links(type IN causal_types)` → `DISTINCT ON` best weight
5. Additive score combination across 3 signals
6. `LinkExpansionNoteGraphStrategy` mirror
7. Register `'link_expansion'` in factories
8. Config field for causal threshold
9. Unit tests: each expansion CTE individually, combined scoring
10. Integration test: facts with entity + semantic + causal links → all discovered

**AC**: Single SQL statement per call. Additive scores [0,3]. Each signal independently contributes. `tanh` normalization on entity counts. `just prek` passes.

---

## T5: Dateparser Temporal Extraction — Size S *(independent)*

**Goal**: Add NLP-based temporal constraint extraction from queries using `dateparser`.

**Rationale**: Memex's `TemporalStrategy` only accepts pre-parsed `start_date`/`end_date` from the API request. It can't parse "last week" or "in March 2024" from natural language queries. Hindsight has `DateparserQueryAnalyzer` that handles this.

**Files**:
- `packages/core/src/memex_core/memory/retrieval/temporal_extraction.py` — New module
- `packages/core/src/memex_core/memory/retrieval/engine.py` — Call extraction before strategy dispatch
- `pyproject.toml` (core package) — Add `dateparser` dependency

**Design**:
```python
# temporal_extraction.py
def extract_temporal_constraint(
    query: str, reference_date: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """Parse natural language temporal expressions. Returns (start, end) or None."""
    import dateparser
    # Use dateparser.search.search_dates() to find temporal expressions
    # Convert to (start_date, end_date) range
    # Handle: "last week", "in March", "yesterday", "3 days ago", etc.
```

**Sub-tasks**:
1. Add `dateparser` to core package dependencies
2. Create `temporal_extraction.py` with `extract_temporal_constraint()`
3. Integrate into `RetrievalEngine.retrieve()` **upstream** of `_perform_rrf_retrieval()` — dates are assembled in the `retrieve()` method before being passed to RRF. Extract temporal constraint from query, merge into the `filters` dict (explicit `start_date`/`end_date` wins).
4. Add `temporal_extraction_enabled: bool = True` config field
5. Unit tests: various temporal expressions → correct date ranges
6. Integration test: query "what happened last week" → temporal strategy receives parsed dates

**AC**: Natural language dates parsed correctly. Explicit `start_date`/`end_date` override extracted ones. Disabled by config flag. No impact when query has no temporal expression. `just prek` passes.

---

## T6: Cross-Encoder Recency + Temporal Boosts — Size S *(independent)*

**Goal**: Add multiplicative recency and temporal proximity boosts to cross-encoder reranking.

**Rationale**: Memex reranking uses raw cross-encoder score only (engine.py lines 704-741). Hindsight applies `combined = ce_norm × recency_boost × temporal_boost` where each boost is `1 ± 10%`. The position-aware blending method in Memex is currently a NO-OP (lines 341-355).

**Reference**: Hindsight `reranking.py` lines 18-69: `_RECENCY_ALPHA = 0.2`, `_TEMPORAL_ALPHA = 0.2`

**Files**:
- `packages/core/src/memex_core/memory/retrieval/engine.py` — Modify `_rerank_results()`
- `packages/common/src/memex_common/config.py` — 2 config fields

**Design**:
```python
# In _rerank_results(), after getting cross-encoder scores:
for unit, ce_score in zip(results, scores):
    # Recency: linear decay over 365 days, [0.1, 1.0], neutral at 0.5
    days_ago = (now - (unit.event_date or now)).days
    recency = max(0.1, min(1.0, 1.0 - (days_ago / 365)))
    recency_boost = 1.0 + recency_alpha * (recency - 0.5)  # [0.9, 1.1]

    # Temporal: use temporal_proximity if available, else neutral
    temporal_boost = 1.0 + temporal_alpha * (temporal - 0.5)  # [0.9, 1.1]

    combined = ce_score * recency_boost * temporal_boost
```

**Sub-tasks**:
1. Add `reranking_recency_alpha: float = 0.2` and `reranking_temporal_alpha: float = 0.2` to `RetrievalConfig`
2. Implement recency calculation (linear decay, 365 days, floor 0.1)
3. Apply multiplicative boosts in `_rerank_results()`
4. Pass `RetrievalConfig` to reranking method
5. Unit tests: verify boost ranges, neutral case (alpha=0), edge cases (no event_date)
6. Remove or implement the NO-OP `_apply_position_aware_blending` (clean up dead code). **Note**: method IS called at line 259 gated on `fusion_strategy == 'position_aware'` — removing it also requires removing the call site and potentially the `fusion_strategy` config option from `RetrievalRequest`.

**AC**: Boosts bounded to [0.9, 1.1] with default alpha. Setting alpha=0 → no boost. Missing event_date → neutral (0.5). `just prek` passes.

---

## T7: Fact-Type Partitioned RRF — Size M *(independent)*

**Goal**: Run retrieval strategies per fact type independently, then fuse results — preventing popular fact types from drowning out rare ones.

**Rationale**: Current RRF (engine.py lines 462-526) treats all fact types (world, event, observation) in a single pool. If world facts dominate, events get pushed out. Hindsight's `retrieve_all_fact_types_parallel()` runs per-type and keeps results separate until final merge.

**Files**:
- `packages/core/src/memex_core/memory/retrieval/engine.py` — Major refactor of `_perform_rrf_retrieval()`
- `packages/common/src/memex_common/config.py` — Config fields

**Design**:
```
For each fact_type in [world, event, observation]:
  1. Run all strategies with WHERE fact_type = X filter
  2. RRF merge within that fact type → top-N per type
  3. Collect per-type results

Final: interleave or weighted merge across fact types
```

**Sub-tasks**:
1. Add `fact_type_partitioned_rrf: bool = False` config field (opt-in)
2. Add `fact_type_budget: int = 20` (per-type candidate limit)
3. Refactor `_perform_rrf_retrieval()` to accept optional `fact_type` filter
4. Create `_perform_partitioned_rrf()` that loops over fact types and calls `_perform_rrf_retrieval()` per type. Use `asyncio.gather()` to run per-type queries in parallel (3 sequential queries would triple DB roundtrips). **Note**: `MentalModelStrategy` explicitly skips `apply_generic_filters` (line 347-349) because mental models have no `fact_type` — handle mental models outside the per-type loop (run once, merge into final results).
5. Merge per-type results: interleave round-robin or weighted by type priority
6. Ensure debug mode works with partitioned path
7. Unit tests: verify per-type isolation, merge order
8. Integration test: insert mixed fact types → verify balanced representation in results

**AC**: Opt-in via config (default off — no behavioral change). Each fact type gets independent ranking. No fact type starved in results. Performance acceptable (parallel execution per type). `just prek` passes.

---

## Future: MPFP Meta-Path Forward Push — Size L

Sublinear graph traversal with 7 predefined meta-path patterns (`[semantic,semantic]`, `[entity,temporal]`, `[semantic,causes]`, etc.) and Forward Push mass propagation with α=0.15 teleport. Complex implementation requiring `EdgeCache` with asyncio locks, hop-synchronized edge loading, and per-pattern budget allocation. Filed as future research item in memex vault.

---

## Key Files

| File | Role |
|------|------|
| `packages/core/src/memex_core/memory/retrieval/strategies.py` | All strategy classes + factories |
| `packages/core/src/memex_core/memory/retrieval/engine.py` | `RetrievalEngine`, RRF, reranking |
| `packages/core/src/memex_core/memory/retrieval/document_search.py` | `NoteSearchEngine` |
| `packages/common/src/memex_common/config.py` | `RetrievalConfig` |
| `packages/core/src/memex_core/api.py` | Config passthrough |
| `packages/core/src/memex_core/memory/sql_models.py` | `MemoryLink`, `MemoryUnit`, `UnitEntity` |
| `packages/core/src/memex_core/memory/retrieval/temporal_extraction.py` | New: NLP temporal parsing |

## Verification

1. `just prek` — ruff + mypy pass (after each ticket)
2. `uv run pytest packages/core/tests/unit/memory/retrieval/ -v` — unit tests
3. `uv run pytest packages/common/tests/ -v` — config tests
4. `uv run pytest packages/core/tests/integration/memory/retrieval/ -v -m integration` — integration tests
5. Default behavior: all new features off or backward-compatible by default
