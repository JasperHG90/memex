# About Retrieval Strategies

Memex retrieves memories using TEMPR — five independent strategies that run in parallel and are fused into a single ranked result. This document explains how each strategy works, why multiple strategies matter, and how they are combined.

## Context

No single retrieval method works well for all queries. A keyword search for "PostgreSQL" will miss facts about "database migration" that do not mention PostgreSQL by name. A semantic search for "deployment decisions" might rank irrelevant but semantically similar content too high. By running multiple strategies and combining their results, Memex achieves robust retrieval across a wide range of query types.

## The Five Strategies

### 1. Semantic Strategy (Dense Retrieval)

**What it does:** Computes the cosine distance between the query embedding and each memory unit's embedding. Results with smaller distance (more similar vectors) rank higher.

**When it shines:** Conceptual queries where the exact words do not appear in the stored facts. "What are the architectural trade-offs?" will match facts about "we chose eventual consistency over strong consistency" even though no words overlap.

**How it works in SQL:**

```sql
SELECT id, embedding <=> $query_embedding AS score
FROM memory_units
ORDER BY score ASC  -- cosine distance: lower = more similar
LIMIT 60
```

The strategy uses `pgvector`'s cosine distance operator (`<=>`). It requires a pre-computed query embedding.

### 2. Keyword Strategy (Sparse Retrieval)

**What it does:** Full-text search using PostgreSQL's `ts_rank_cd` with stemmed, lemmatized query terms. Uses a "bag of words" approach where query terms are OR'd together (not AND'd) to simulate BM25's inclusive matching.

**When it shines:** Precise queries with specific terms. "SELECT FOR UPDATE SKIP LOCKED" will find the exact fact about PostgreSQL advisory locks, which semantic search might miss in favor of generically similar content.

**How it works:**

```sql
SELECT id, ts_rank_cd(to_tsvector('english', text), query) AS score
FROM memory_units
WHERE to_tsvector('english', text) @@ query
ORDER BY score DESC
LIMIT 60
```

The reason Memex uses OR-based matching rather than AND is that natural language queries often contain words that do not all appear in any single fact. "deployment architecture decisions" should match facts that mention "deployment" even if they don't also mention "architecture".

### 3. Graph Strategy (Entity Traversal)

**What it does:** Identifies entities mentioned in the query, then traverses the knowledge graph to find memory units linked to those entities (1st order) and entities related to those entities (2nd order via co-occurrence).

**When it shines:** Entity-centric queries. "What do we know about the Kubernetes migration?" uses NER to extract "Kubernetes", finds the entity, and retrieves all facts linked to it — including facts that mention "k8s" (via alias matching) or "container orchestration" (via co-occurrence).

**Entity resolution pipeline:**

1. **NER extraction**: A fast NER model extracts entity names from the query.
2. **Canonical matching**: Exact match against `Entity.canonical_name`.
3. **Phonetic matching**: Metaphone codes handle misspellings ("Kubernetees").
4. **Fuzzy matching**: PostgreSQL `pg_trgm` similarity catches partial matches.
5. **Alias matching**: Checks `EntityAlias` table for known aliases.

**Scoring:**

- *1st order* (direct entity link): `score = 1.0 + temporal_decay`, where temporal decay is `base ^ (-days / decay_days)` (default: `2 ^ (-days / 30)`).
- *2nd order* (co-occurrence): `score = ln(cooccurrence_count + 1) / ln(neighbor_mention_count + 2)`. This formula penalizes "hub" entities (high mention count) and rewards strong co-occurrence relationships.

### 4. Temporal Strategy

**What it does:** Ranks memory units purely by recency. The most recently created or referenced facts appear first.

**When it shines:** Queries about current state. "What happened this week?" or "latest project status" benefit from recent facts being prioritized regardless of semantic relevance.

**How it works:**

```sql
SELECT id, EXTRACT(epoch FROM event_date) AS score
FROM memory_units
ORDER BY event_date DESC
LIMIT 60
```

The temporal strategy acts as a recency bias in the fusion. Even when other strategies find an older fact more relevant, the temporal contribution ensures recent information is not buried.

### 5. Mental Model Strategy

**What it does:** Searches the mental models table (synthesized observations from the reflection engine) using vector similarity on the model's summary embedding.

**When it shines:** High-level, conceptual queries. "What is the team's approach to testing?" is best answered by a mental model that synthesizes dozens of individual testing-related facts into a coherent summary, rather than returning 10 individual facts about specific tests.

**Note:** Mental models are only available if the reflection engine has run. For new Memex instances with few ingested documents, this strategy may not contribute results.

## Reciprocal Rank Fusion (RRF)

All five strategies contribute ranked candidate lists. Memex combines them using Reciprocal Rank Fusion, a robust fusion algorithm that does not require score calibration between strategies.

**Formula:**

```
RRF_score(item) = SUM over strategies: weight / (k + rank)
```

Where:
- `k` is the RRF constant (default: 60, configurable via `retrieval.rrf_k`)
- `rank` is the item's 1-based position in that strategy's result list
- `weight` is the query weight (original query: 2.0, expanded queries: 1.0)

The reason RRF works well is that it depends only on *rank*, not on raw scores. This means that a cosine distance of 0.15 from the semantic strategy and a ts_rank of 3.7 from the keyword strategy can be meaningfully combined without any normalization.

**Implementation:** Memex performs RRF entirely in SQL using Common Table Expressions (CTEs). Each strategy's results are unioned into a single candidates table with assigned ranks, then grouped and summed to produce the final RRF score. This avoids round-tripping data between Python and PostgreSQL.

### Multi-Query Expansion

When `expand_query` is enabled, Memex uses an LLM to generate query variations before retrieval. For example, "deployment architecture" might expand to:

- "infrastructure design decisions"
- "production environment setup"

Each variation runs through all five strategies independently. Results are then fused together, with the original query weighted 2x higher than expansions.

## Post-Fusion Pipeline

After RRF fusion produces a single ranked list, results pass through several post-processing steps before being returned. These steps refine the ranking based on signals that are orthogonal to relevance.

### MMR Diversity Filtering

After RRF fusion, Maximal Marginal Relevance (MMR) filtering removes near-duplicate results. This is especially important when multiple retrieval strategies surface the same cluster of facts — for example, 10 facts about Python classes from a single tutorial note.

**The MMR formula:**

```
MMR(candidate) = λ × relevance − (1 − λ) × max_similarity(candidate, already_selected)
```

Where:
- `λ` (lambda) controls the trade-off: `1.0` = pure relevance (no diversity), `0.0` = max diversity
- Default: `0.9` (conservative — only suppresses near-duplicates)

**Hybrid similarity kernel:**

MMR uses a hybrid similarity measure that combines two signals:

| Signal | Weight | Method |
|:-------|:-------|:-------|
| Embedding cosine | `0.6` | `1 - (a.embedding <=> b.embedding)` via pgvector |
| Entity Jaccard | `0.4` | `|entities_a ∩ entities_b| / |entities_a ∪ entities_b|` |

This means two facts are considered "similar" if they have both similar vector representations *and* mention the same entities. The entity Jaccard component catches cases where facts are semantically similar but about different subjects.

When two candidates have identical MMR scores, a temporal tiebreaker (ε=0.01 × recency) favors more recent facts.

### Cross-Encoder Reranking

The top results can optionally pass through a cross-encoder reranker for more precise scoring. The reranker evaluates each (query, fact) pair directly, which is more accurate than embedding similarity but too slow to run on all candidates.

## Debug Mode

When `debug=True` is passed to a search request, the retrieval engine logs detailed information about each strategy's contribution:

- Per-strategy timing (milliseconds)
- Per-result breakdown showing which strategies contributed and their individual RRF scores
- Raw scores from each strategy before fusion

This is useful for understanding why certain results rank higher and for tuning strategy weights.

## Configuration

Key retrieval settings in `config.yaml`:

```yaml
server:
  memory:
    retrieval:
      token_budget: 2000                # Max tokens to pack into results
      rrf_k: 60                         # RRF constant (higher = more uniform blending)
      candidate_pool_size: 60           # Candidates per strategy
      similarity_threshold: 0.3         # pg_trgm threshold for entity matching
      temporal_decay_days: 30.0         # Half-life for temporal scoring
      temporal_decay_base: 2.0          # Exponential base for decay
      mmr_lambda: 0.9                   # MMR diversity (null=disabled, 0.9=conservative)
      mmr_embedding_weight: 0.6         # Cosine weight in hybrid similarity kernel
      mmr_entity_weight: 0.4            # Entity Jaccard weight in hybrid similarity kernel
      retrieval_strategies:
        semantic: true
        keyword: true
        graph: true
        temporal: true
        mental_model: true
```

Individual strategies can be toggled on or off. Disabling a strategy is useful for debugging (isolating which strategy contributes specific results) or for performance in environments where certain strategies are unnecessary.

## See Also

* [About the Hindsight Framework](hindsight-framework.md) — overall architecture
* [About Reflection and Mental Models](reflection-and-mental-models.md) — how mental models are created
* [How to Choose Between Document Search and Memory Search](../how-to/doc-search-vs-memory-search.md) — practical search guidance
