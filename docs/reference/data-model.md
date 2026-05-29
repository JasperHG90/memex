# Data model

The Memex Postgres schema. Every persistent table, every column, every foreign key. Source of truth: `packages/core/src/memex_core/memory/sql_models.py`. The schema is also re-created by Alembic migrations under `packages/core/src/memex_core/alembic/versions/`; the SQLModel classes and the migrations stay in lockstep, and `Base.metadata.create_all` (test path) lays down the same indices the migrations create.

Conventions used throughout:

- **Vault-scoped** means the row carries a `vault_id` foreign key to `vaults.id` with `ON DELETE CASCADE`. The default is the well-known global vault UUID `ac9b6a45-d388-5ddb-9fa9-50d4e5bca511`. <code-ref path="packages/common/src/memex_common/config.py" lines="31" />
- **`created_at` / `updated_at`** use the shared mixin: `TIMESTAMP WITH TIME ZONE`, server default `now()`, and `updated_at` carries `ON UPDATE now()`. <code-ref path="packages/core/src/memex_core/memory/mixins.py" lines="16-26" />
- **`Vector(384)`** columns hold pgvector embeddings at the project-wide dimension `EMBEDDING_DIMENSION = 384`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="33" />
- **Numeric defaults** in this doc (intent-class stability days, importance values, exploration weights) reflect the schema's `server_default` and column comments; they may be tuned per deployment in code outside the schema. Treat them as empirical anchors, not contracts.

## Table index

| Table | Vault-scoped? | Primary purpose |
|-------|---------------|-----------------|
| `vaults` | — (root) | Logical isolation boundary for multi-tenancy / project separation. |
| `notes` | yes | Raw ingested documents (file, email, chat log, web page). |
| `chunks` | yes | Content-addressed paragraph blocks; carry the embedding for block-level search. |
| `nodes` | yes | Section-level text units from PageIndex; aggregate into chunks. |
| `memory_units` | yes | Append-only facts extracted from chunks. The "Hindsight Facts" table. |
| `entities` | — (global) | People, places, organizations, concepts — nodes of the knowledge graph. |
| `entity_aliases` | — (global) | Nicknames, abbreviations, former names mapped to a canonical entity. |
| `unit_entities` | yes | Join table: which entities are mentioned in which memory units. |
| `entity_cooccurrences` | yes | Cached count of how often two entities co-occur. The graph edge table. |
| `mental_models` | yes | Per-entity synthesized "mental model" — observations, trends, narrative. |
| `memory_links` | yes | Typed edges between memory units (temporal, causes, contradicts, etc.). |
| `reflection_queue` | yes | Queue of entities needing reflection or observation refresh. |
| `batch_jobs` | yes | Status tracker for asynchronous batch ingestion jobs. |
| `kv_entries` | — (key-namespaced) | Key-value store: preferences, conventions, procedures, settings. |
| `vault_summaries` | yes (1:1) | Evolving thematic synthesis of a vault's contents. |
| `note_appends` | yes (via note) | Audit row per atomic append to an existing note; idempotency token. |
| `audit_logs` | — (global) | Append-only security audit trail. |
| `outcome_audit_log` | yes | One row per `record_outcome` call; per-unit verb payload. |
| `maintenance_proposals` | optional | Finding ledger emitted by the LintService (nullable vault_id). |
| `consolidation_ticks` | yes | One row per consolidation tick; orchestrator audit. |
| `lint_llm_quota` | yes | Hour-bucket counter for the 24-hour rolling LLM-lint cost cap. |

## `vaults`

Logical isolation boundary. Every vault-scoped table foreign-keys to this row with `ON DELETE CASCADE` — dropping a vault cascades to every note, chunk, memory unit, link, and queue entry that referenced it. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="50-80" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `name` | text | no | — | Unique. Indexed. |
| `description` | text | yes | NULL | Free-form. |
| `mw_mode` | text | no | `'stationary'` | Memory Worth counter mode for this vault. |
| `created_at` | timestamptz | no | `now()` | Row insertion. |

CHECK constraints:

- `vaults_mw_mode_check`: `mw_mode IN ('stationary', 'ema')`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="75-80" />

## `notes`

The raw container for ingested information. One `Note` row becomes one or more `Chunk` rows (and `Node` rows when PageIndex extraction is used), then many `MemoryUnit` rows. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="210-353" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | — | Primary key. Caller-supplied. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `session_id` | text | no | from request context | Indexed. `'global'` if no session in scope. |
| `title` | text | yes | NULL | Resolved human-readable title. |
| `description` | text | yes | NULL | Short synthesized description. |
| `original_text` | text | yes | NULL | Full raw body. |
| `page_index` | jsonb | yes | NULL | Thin TOC tree; populated only when the page-index extraction strategy ran. |
| `content_hash` | text | yes | NULL | MD5 of `original_text`. Used for dedupe. |
| `filestore_path` | text | yes | NULL | Path to the source file in the file store. |
| `assets` | text[] | no | `ARRAY[]::text[]` | Associated asset paths (images, PDFs). |
| `metadata` | jsonb | no | `'{}'` | Maps to the Python attribute `doc_metadata`. Arbitrary source info. |
| `publish_date` | timestamptz | yes | NULL | Indexed. Publication or event date. |
| `status` | text | no | `'active'` | Indexed. Lifecycle: `'active'` or `'superseded'`. |
| `superseded_by` | UUID | yes | NULL | ID of the note that supersedes this one. |
| `appended_to` | UUID | yes | NULL | ID of the note this one was appended to. |
| `archived_at` | timestamptz | yes | NULL | Indexed. Non-NULL means the note was archived; the cascade lives in FSFM via `MemoryUnit.is_deprioritized=true`, not in `status`. |
| `summary_version_incorporated` | int | yes | NULL | `VaultSummary.version` when this note was last folded into the summary. NULL or `< current version` means pending. |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` (also `ON UPDATE`) | |

Indices:

- `idx_notes_content_hash` on `(content_hash)`.
- `idx_notes_title_trgm` GIN on `lower(title) gin_trgm_ops` — substring/title-fragment matching.
- `idx_notes_summary_version` on `(vault_id, summary_version_incorporated)` — drives the "which notes still need to be folded into the summary?" scan.

CHECK constraints:

- `ck_notes_status`: `status IN ('active', 'superseded')`.

Cascades:

- Deleting a note cascades-delete its `memory_units` and `chunks` (declared via SQLModel `Relationship` with `cascade='all, delete-orphan'`).

## `chunks`

Content-addressed paragraph blocks. The chunk carries the embedding; nodes inside the chunk carry the prose. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="356-440" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `note_id` | UUID | no | — | FK → `notes.id`, `ON DELETE CASCADE`. Indexed. |
| `text` | text | no | — | Raw paragraph text. |
| `content_hash` | text | no | `''` | SHA-256 of whitespace-normalized text. |
| `status` | text | no | `'active'` | `'active'` or `'stale'`. |
| `embedding` | vector(384) | yes | NULL | pgvector. HNSW-indexed for cosine similarity. |
| `chunk_index` | int | no | — | Sequential order within the note. |
| `summary` | jsonb | yes | NULL | Block-level summary: `{"topic": ..., "key_points": [...]}`. |
| `summary_formatted` | text | yes | NULL | Pre-formatted: `"topic — point1 \| point2 \| ..."`. |
| `created_at` | timestamptz | no | `now()` | |

Indices:

- `idx_chunks_note_id` on `(note_id)`.
- `idx_chunks_note_index` on `(note_id, chunk_index)`.
- `idx_chunks_text_tsvector` GIN on `to_tsvector('english', text)` — full-text search.
- `idx_chunks_embedding` HNSW on `embedding` with `vector_cosine_ops`.

CHECK / UNIQUE constraints:

- `chunks_status_check`: `status IN ('active', 'stale')`.
- `uq_chunks_note_content_hash` UNIQUE `(note_id, content_hash)` — same hash within a note collapses to one row.

JSON shape — `summary`:

```json
{"topic": "JWT rotation cadence", "key_points": ["7-day window", "two-key overlap during cutover"]}
```

## `nodes`

Section-level text units produced by the PageIndex extraction strategy. Each node maps to a section/subsection in the document hierarchy; many nodes aggregate into one `Chunk`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="443-536" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `note_id` | UUID | no | — | FK → `notes.id`, `ON DELETE CASCADE`. Indexed. |
| `block_id` | UUID | yes | NULL | FK → `chunks.id`, `ON DELETE SET NULL`. Indexed. NULL until block assignment. |
| `node_hash` | text | no | — | MD5 of node content for incremental diffing. |
| `title` | text | no | — | Section title. |
| `text` | text | no | — | Full text content of the node. |
| `summary` | jsonb | yes | NULL | `SectionSummary` blob: `{"who": ..., "what": ..., "how": ..., "when": ..., "where": ...}`. |
| `summary_formatted` | text | yes | NULL | Pre-formatted: `"who \| what \| how \| when \| where"`. |
| `level` | int | no | — | Hierarchy level (1 = H1, 2 = H2, …). |
| `seq` | int | no | — | Sequential order within the document. |
| `token_estimate` | int | no | `0` | Token count of `text`. |
| `status` | text | no | `'active'` | `'active'` or `'stale'`. |
| `created_at` | timestamptz | no | `now()` | |

Indices:

- `idx_nodes_note_id` on `(note_id)`.
- `idx_nodes_block_id` on `(block_id)`.
- `idx_nodes_text_tsvector` GIN on `to_tsvector('english', text)`.

CHECK / UNIQUE constraints:

- `nodes_status_check`: `status IN ('active', 'stale')`.
- `uq_nodes_note_node_hash` UNIQUE `(note_id, node_hash)`.

## `memory_units`

The append-only fact table. The Hindsight "Facts" concept. Memory units extend `MemoryUnitBase` from `memex_common.schemas` — the inheritance is overridden here to attach SQLModel column types. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="539-832" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `text` | text | no | — | The fact body. |
| `fact_type` | text | no | `'world'` | One of `'world'`, `'event'`, `'observation'`. |
| `occurred_start` | timestamptz | yes | NULL | Start of the time interval the fact refers to. |
| `occurred_end` | timestamptz | yes | NULL | End of the interval. |
| `mentioned_at` | timestamptz | yes | NULL | When the fact was mentioned in the source. |
| `note_id` | UUID | yes | NULL | FK → `notes.id`, `ON DELETE CASCADE`. Indexed. |
| `chunk_id` | UUID | yes | NULL | FK → `chunks.id`, `ON DELETE SET NULL`. Indexed. |
| `status` | text | no | `'active'` | `'active'` or `'stale'`. Indexed. |
| `embedding` | vector(384) | yes | NULL | HNSW-indexed. |
| `context` | text | yes | NULL | Free-form context. Partial index when non-NULL. |
| `event_date` | timestamptz | no | — | Date the unit is anchored to. Indexed `DESC`. |
| `success_co_count` | int | no | `0` | Memory Worth success counter (vault-scoped). |
| `failure_co_count` | int | no | `0` | Memory Worth failure counter. |
| `unused_co_count` | int | no | `0` | Engagement counter — retrieved but marked `not_used`. Does NOT enter the Beta-Bernoulli posterior. |
| `is_deprioritized` | bool | no | `false` | Non-destructive retrieval downweight. Partial-indexed when `true`. |
| `intent_class` | text | no | `'durable'` | `'permanent'`, `'durable'`, or `'ephemeral'`. |
| `risk_class` | text | no | `'none'` | `'none'`, `'sensitive'`, `'private'`, or `'safety'`. |
| `claim_type` | text | yes | NULL | Corrective-claim signal: `'resolution'`, `'contradiction'`, or NULL. |
| `confidence` | float | no | `1.0` | Range 0.0–1.0. Decreased when contradicted by newer information. |
| `confidence_evidence_count` | int | no | `0` | Negative-evidence event count. Pairs with the closed-form Beta(1,1) posterior in `memex_core.memory.confidence.mean_and_variance` to derive variance without storing it. Cold-start (`count = 0`) → variance = 1/12. |
| `importance` | float | yes | NULL | Derived from `intent_class` at write time: `permanent=1.0`, `durable=0.7`, `ephemeral=0.3`. NULL → neutral 1.0 in the decay boost. |
| `stability` | float | yes | NULL | Stability in days per intent class: `durable=180`, `ephemeral=14`, `permanent=NULL` (infinity). NULL → decay term = 1.0. |
| `last_outcome_at` | timestamptz | yes | NULL | Wall-clock of the most recent `record_outcome`. NULL → no temporal anchor → neutral 1.0. |
| `metadata` | jsonb | no | `'{}'` | Maps to Python attribute `unit_metadata`. |
| `search_tsvector` | tsvector | yes | computed | Generated, persisted column: `to_tsvector('english', text \|\| metadata->>'tags' \|\| metadata->>'enriched_tags' \|\| metadata->>'enriched_keywords')`. |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` (also `ON UPDATE`) | |

Indices:

- `idx_memory_units_note_id` on `(note_id)`.
- `idx_memory_units_chunk_id` on `(chunk_id)`.
- `idx_memory_units_status` on `(status)`.
- `idx_memory_units_event_date` on `(event_date DESC)`.
- `idx_memory_units_is_deprioritized` on `(is_deprioritized)` — partial, `WHERE is_deprioritized = true`.
- `idx_memory_units_fact_type` on `(fact_type)`.
- `idx_memory_units_confidence` on `(confidence)`.
- `idx_memory_units_embedding` HNSW on `embedding` with `vector_cosine_ops`.
- `idx_memory_units_embedding_active` HNSW partial, `WHERE status = 'active'`.
- `idx_memory_units_embedding_stale` HNSW partial, `WHERE status = 'stale'`.
- `idx_memory_units_search_tsvector` GIN on `search_tsvector`.
- `ix_memory_units_context` on `(context)` partial, `WHERE context IS NOT NULL`.

CHECK constraints:

- `fact_type IN ('world', 'event', 'observation')`.
- `memory_units_status_check`: `status IN ('active', 'stale')`.
- `ck_memory_units_intent_class`: `intent_class IN ('permanent', 'durable', 'ephemeral')`.
- `ck_memory_units_risk_class`: `risk_class IN ('none', 'sensitive', 'private', 'safety')`.
- `ck_memory_units_claim_type`: `claim_type IS NULL OR claim_type IN ('resolution', 'contradiction')`.
- `memory_units_confidence_check`: `confidence >= 0.0 AND confidence <= 1.0`.
- `memory_units_confidence_evidence_count_check`: `confidence_evidence_count >= 0`.

JSON shape — `metadata` (selected keys consumed by the retrieval engine):

```json
{
  "tags": "deploy ci-cd",
  "enriched_tags": "deployment automation",
  "enriched_keywords": "rollback github-actions",
  "citations": [{"text": "...", "date": "2026-03-12T00:00:00Z"}],
  "virtual": false
}
```

The `virtual: true` flag marks read-only observation projections of memory units (mental-model surface). Calls to `memex_memory_deprioritize` on a virtual unit return HTTP 400 with `source_memory_units` listing the underlying MU IDs to suppress instead.

## `entities`

Knowledge-graph nodes. Entities are global (not vault-scoped); the per-vault edge structure lives in `unit_entities` and `entity_cooccurrences`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="865-966" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `canonical_name` | text | no | — | Standardized name. Unique. |
| `phonetic_code` | text | yes | NULL | Double Metaphone code. Indexed. |
| `entity_type` | text | yes | NULL | NER-derived: Person, Organization, Location, Concept. |
| `first_seen` | timestamptz | no | `now()` | First mention in the corpus. |
| `last_seen` | timestamptz | no | `now()` | Most recent mention. |
| `mention_count` | int | no | `1` | Cumulative mention count. |
| `retrieval_count` | int | no | `0` | Cumulative count of retrievals returning this entity. |
| `last_retrieved_at` | timestamptz | yes | NULL | Most recent retrieval timestamp. |
| `last_merge_scan_at` | timestamptz | yes | NULL | Most recent cross-batch entity-merge scan; NULL = never scanned. |

Indices:

- `idx_entities_canonical_name_unique` UNIQUE on `(canonical_name)`.
- `idx_entities_canonical_name_trgm` GIN on `lower(canonical_name) gin_trgm_ops`.
- `idx_entities_last_merge_scan_at` on `(last_merge_scan_at)` partial, `WHERE last_merge_scan_at IS NOT NULL`.

## `entity_aliases`

Alternate names that resolve to a canonical entity. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="969-1005" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `canonical_id` | UUID | no | — | FK → `entities.id`, `ON DELETE CASCADE`. |
| `name` | text | no | — | The alias. |
| `phonetic_code` | text | yes | NULL | Indexed. |

Indices:

- `idx_entity_aliases_canonical_name_unique` UNIQUE on `(canonical_id, name)`.
- `idx_entity_aliases_name_trgm` GIN on `lower(name) gin_trgm_ops`.

## `unit_entities`

Many-to-many join between memory units and entities. Composite primary key `(unit_id, entity_id)`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1008-1063" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `unit_id` | UUID | no | — | PK part. FK → `memory_units.id`, `ON DELETE CASCADE`. |
| `entity_id` | UUID | no | — | PK part. FK → `entities.id`, `ON DELETE CASCADE`. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `success_co_count` | int | no | `0` | Per-edge Memory Worth success counter. |
| `failure_co_count` | int | no | `0` | Per-edge failure counter. |
| `unused_co_count` | int | no | `0` | Per-edge engagement-only counter. |

Indices:

- `idx_unit_entities_unit` on `(unit_id)`.
- `idx_unit_entities_entity` on `(entity_id)`.

## `entity_cooccurrences`

Cached, undirected edge between two entities — the graph "edge" table. The CHECK constraint `entity_id_1 < entity_id_2` forces every pair into one canonical row (no duplicate "A+B" vs "B+A"). <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1066-1151" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `entity_id_1` | UUID | no | — | PK part. FK → `entities.id`, `ON DELETE CASCADE`. Lexicographically smaller. |
| `entity_id_2` | UUID | no | — | PK part. FK → `entities.id`, `ON DELETE CASCADE`. Lexicographically larger. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `cooccurrence_count` | int | no | `1` | Count of joint appearances. |
| `last_cooccurred` | timestamptz | no | `now()` | Most recent joint mention. |
| `valid_from` | timestamptz | yes | NULL | Start of the relation's validity. NULL = open-start. |
| `valid_to` | timestamptz | yes | NULL | End of validity. NULL = still valid. |

Indices:

- `idx_entity_cooccurrences_entity1` on `(entity_id_1)`.
- `idx_entity_cooccurrences_entity2` on `(entity_id_2)`.
- `idx_entity_cooccurrences_count` on `(cooccurrence_count DESC)`.
- `idx_entity_cooccurrences_temporal` on `(entity_id_1, entity_id_2, valid_to DESC NULLS FIRST, valid_from DESC)`.

CHECK constraints:

- `entity_cooccurrence_order_check`: `entity_id_1 < entity_id_2`.

## `mental_models`

Synthesized per-entity "mental model": a list of observations, structured metadata, an embedding centroid, and Memory Worth counters. One row per `(entity_id, vault_id)`. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="124-207" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `entity_id` | UUID | no | — | The entity this model describes. Indexed. |
| `name` | str | no | — | Canonical name of the entity. |
| `observations` | jsonb | no | `'[]'` | List of `Observation` blobs (see JSON shape below). |
| `entity_metadata` | jsonb | no | `'{}'` | Structured metadata derived from observations: description, category, status. |
| `last_refreshed` | timestamptz | no | `now()` | When the reflection engine last updated this row. |
| `version` | int | no | `1` | Incremented on each update. |
| `embedding` | vector(384) | yes | NULL | Centroid of observation embeddings. |
| `success_co_count` | int | no | `0` | Memory Worth success counter. |
| `failure_co_count` | int | no | `0` | Failure counter. |
| `unused_co_count` | int | no | `0` | Engagement-only counter. |

Indices:

- `idx_mental_models_entity_vault_unique` UNIQUE on `(entity_id, vault_id)`.
- `idx_mental_models_observations_gin` GIN on `observations` with `jsonb_path_ops` — supports vault-scoped scans for observations citing a deprioritized memory unit (the deprio → refresh-task enqueue path).

JSON shape — `observations[]` (one element):

```json
{
  "id": "5b8e...",
  "title": "Prefers small, frequent commits",
  "content": "Stated in three separate sessions...",
  "trend": "stable",
  "evidence": [
    {
      "memory_id": "9af3...",
      "quote": "I like to commit each logical change separately.",
      "relevance": 1.0,
      "explanation": "Direct stated preference.",
      "timestamp": "2026-04-10T11:00:00Z"
    }
  ]
}
```

`trend` is one of `'new'`, `'stable'`, `'strengthening'`, `'weakening'`, `'stale'`. The `evidence[]` list may include STALE memory units (those superseded by a newer contradicting note) — STALE evidence remains cited as historical support and is NOT auto-pruned. Treat it as audit trail, not active claim.

## `memory_links`

Typed edges between memory units. Composite primary key `(from_unit_id, to_unit_id, link_type)` — the same pair may carry multiple typed edges. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1409-1501" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `from_unit_id` | UUID | no | — | PK part. FK → `memory_units.id`, `ON DELETE CASCADE`. |
| `to_unit_id` | UUID | no | — | PK part. FK → `memory_units.id`, `ON DELETE CASCADE`. |
| `link_type` | text | no | — | PK part. See enum below. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `entity_id` | UUID | yes | NULL | Optional FK → `entities.id`, `ON DELETE CASCADE`. |
| `link_metadata` | jsonb | no | `'{}'` | Structured metadata (e.g. supersession provenance). |
| `weight` | float | no | `1.0` | Strength or certainty, range 0.0–1.0. |
| `created_at` | timestamptz | no | `now()` | |

Indices:

- `idx_memory_links_from` on `(from_unit_id)`.
- `idx_memory_links_to` on `(to_unit_id)`.
- `idx_memory_links_type` on `(link_type)`.
- `idx_memory_links_link_type_to_unit` on `(link_type, to_unit_id)` — mirrors the migration-side index name in `033_confidence_evidence_count.py`.
- `idx_memory_links_entity` on `(entity_id)` partial, `WHERE entity_id IS NOT NULL`.
- `idx_memory_links_from_weight` on `(from_unit_id, weight DESC)` partial, `WHERE weight >= 0.1`.

CHECK constraints:

- `memory_links_link_type_check`: `link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', 'enables', 'prevents', 'reinforces', 'weakens', 'contradicts', 'refines')`.
- `memory_links_weight_check`: `weight >= 0.0 AND weight <= 1.0`.

## `reflection_queue`

The deferred work queue for the reflection engine. Workers claim rows via `SELECT ... FOR UPDATE SKIP LOCKED` with leader election via Postgres advisory locks. Two task types share the table: full entity reflection (Phases 0–6) and surgical observation refresh after a memory-unit deprioritization. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1270-1406" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `entity_id` | UUID | no | — | FK → `entities.id`, `ON DELETE CASCADE`. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `priority_score` | float | no | `1.0` | Urgency: accumulated evidence + graph centrality + retrieval resonance. |
| `accumulated_evidence` | int | no | `0` | Count of new memory units since last reflection. |
| `status` | text | no | `'pending'` | `'pending'`, `'processing'`, `'failed'`, or `'dead_letter'`. |
| `last_queued_at` | timestamptz | no | `now()` | Most recent enqueue or update. |
| `retry_count` | int | no | `0` | Failures so far. |
| `max_retries` | int | no | `3` | Move to dead letter after this many failures. |
| `last_error` | text | yes | NULL | Most recent failure message. |
| `task_type` | text | no | `'reflect'` | `'reflect'` = full Phase 0–6; `'refresh_observation'` = surgical resync. |
| `observation_id` | UUID | yes | NULL | Refresh payload: the observation in `mental_models.observations` to refresh. |
| `priority_lane` | bool | no | `false` | True for refresh tasks and restore-driven priority reflects; claimed ahead of regular tasks. |
| `source_unit_id` | UUID | yes | NULL | The MU whose deprioritization triggered the refresh. Used by the post-lock sibling-query re-check. |

Indices:

- `idx_reflection_queue_priority` on `(priority_score DESC)`.
- `idx_reflection_queue_status` on `(status)`.
- `idx_reflection_queue_lane_priority` on `(priority_lane DESC, priority_score DESC, last_queued_at)` partial, `WHERE status IN ('pending', 'failed')` — the leader-election claim path.
- `idx_reflection_queue_refresh_unique` UNIQUE on `(entity_id, vault_id, observation_id)` partial, `WHERE task_type = 'refresh_observation' AND status IN ('pending', 'processing')` — at most one active refresh per observation.
- `idx_reflection_queue_entity_vault_active_unique` UNIQUE on `(entity_id, vault_id)` partial, `WHERE task_type = 'reflect' AND status IN ('pending', 'processing')` — at most one active full reflection per entity.

CHECK constraints:

- `status IN ('pending', 'processing', 'failed', 'dead_letter')`.
- `ck_reflection_queue_task_type`: `task_type IN ('reflect', 'refresh_observation')`.

## `batch_jobs`

Status tracker for asynchronous batch ingestion. The `input_note_keys` array lets `JobManager.create_job` detect overlap with concurrent pending/processing jobs and return HTTP 409 instead of starting a duplicate. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1172-1267" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `status` | text | no | `'pending'` | `'pending'`, `'processing'`, `'completed'`, or `'failed'`. Indexed. |
| `progress` | text | yes | NULL | Human-readable progress string. |
| `result` | jsonb | no | `'{}'` | Serialized `BatchIngestResponse` on completion. |
| `notes_count` | int | no | `0` | Total notes in the batch. |
| `processed_count` | int | no | `0` | |
| `skipped_count` | int | no | `0` | |
| `failed_count` | int | no | `0` | |
| `note_ids` | jsonb | no | `'[]'` | List of created Note UUIDs. |
| `input_note_keys` | jsonb | no | `'[]'` | Sorted, deduped list of idempotency keys for incoming notes. Set once at row creation; never updated. |
| `error_info` | jsonb | yes | NULL | Detailed error payload. |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` (also `ON UPDATE`) | |
| `started_at` | timestamptz | yes | NULL | When processing began. |
| `completed_at` | timestamptz | yes | NULL | When processing finished (success or failure). |

Indices:

- `idx_batch_jobs_status` on `(status)`.

## `kv_entries`

Namespaced key-value store. Keys MUST start with one of four namespace prefixes: `global:`, `user:`, `project:`, or `app:`. The btree index uses `text_pattern_ops` so prefix queries (`WHERE key LIKE 'project:abc:%'`) hit the index. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1638-1696" /> Procedural observations live UNDER one of these four scopes as `<scope>:procedure:<verb>:<context-tag>` — bare `procedure:` is rejected (see migration `046_procedure_to_global` which rewrites any legacy bare keys on upgrade).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `key` | text | no | — | Unique. Must carry a namespace prefix. |
| `value` | text | no | — | Stored value. |
| `embedding` | vector(384) | yes | NULL | Optional, for semantic search over values. |
| `expires_at` | timestamptz | yes | NULL | NULL = never expires. Partial-indexed. |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` (also `ON UPDATE`) | |

Indices:

- `uq_kv_key` UNIQUE on `(key)`.
- `idx_kv_key_prefix` btree on `(key)` with `text_pattern_ops` — drives prefix-range scans.
- `idx_kv_expires_at` btree on `(expires_at)` partial, `WHERE expires_at IS NOT NULL`.

KV entries are not vault-scoped at the table level. Scope lives in the key namespace: `user:`, `project:<id>:`, `app:<app-id>:`, or `global:`. Procedure keys are scoped under one of those four namespaces as `<scope>:procedure:<verb>:<context-tag>` (e.g. `global:procedure:deploy:staging`, `project:<id>:procedure:commit:pr-only`).

## `vault_summaries`

One row per vault — enforced by the `UNIQUE` constraint on `vault_id`. Cheap-to-compute thematic overview, updated incrementally on note ingestion or regenerated on demand. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1699-1770" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `vault_id` | UUID | no | — | UNIQUE. FK → `vaults.id`, `ON DELETE CASCADE`. |
| `narrative` | text | no | `''` | Short thematic synthesis (~200 tokens). |
| `themes` | jsonb | no | `'[]'` | List of theme blobs (see shape below). |
| `inventory` | jsonb | no | `'{}'` | Computed content stats. |
| `key_entities` | jsonb | no | `'[]'` | Top entities by mention count. |
| `version` | int | no | `1` | Incremented on each update. |
| `notes_incorporated` | int | no | `0` | Count of notes folded into this summary. |
| `patch_log` | jsonb | no | `'[]'` | Last 20 patches. |
| `needs_regeneration` | bool | no | `false` | Set when notes are deleted/archived; triggers full regeneration on the next scheduler cycle. |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` (also `ON UPDATE`) | |

JSON shapes:

```json
// themes[]
{
  "name": "Deploy pipeline",
  "description": "Notes about CI/CD changes and rollout policy.",
  "note_count": 7,
  "trend": "strengthening",
  "last_addition": "2026-05-04T08:11:00Z",
  "representative_titles": ["ci-cd-circleci-migration", "deploy-window-q4-policy"]
}

// inventory
{
  "total_notes": 142,
  "total_entities": 87,
  "date_range": {"earliest": "2025-09-01", "latest": "2026-05-21"},
  "by_template": {"meeting-note": 23, "design-doc": 6},
  "by_source_domain": {"github.com": 18, "docs.python.org": 5},
  "top_tags": [["deploy", 12], ["security", 8]],
  "recent_activity": [{"date": "2026-05-20", "added": 3}]
}

// key_entities[]
{"name": "Project Hermes", "type": "Project", "mention_count": 41}

// patch_log[]
{"note_id": "...", "action": "add", "timestamp": "...", "delta": {"themes": ["Deploy pipeline"]}}
```

## `note_appends`

One row per atomic delta append to an existing note, keyed on the caller-supplied `append_id` so retries replay the cached outcome without mutating the body twice. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1773-1826" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `append_id` | UUID | no | — | Primary key. Caller-supplied idempotency token. |
| `note_id` | UUID | no | — | FK → `notes.id`, `ON DELETE CASCADE`. |
| `delta_sha256` | text | no | — | SHA-256 of the delta bytes (replay-equality check). |
| `delta_bytes` | int | no | — | Length of the delta in bytes (UTF-8). |
| `joiner` | text | no | — | Separator placed between parent body and delta. |
| `resulting_content_hash` | text | no | — | `content_hash` of the note after the append committed. |
| `new_unit_ids` | uuid[] | no | `ARRAY[]::uuid[]` | Memory units newly extracted from the delta. |
| `applied_at` | timestamptz | no | `now()` | |

Indices:

- `idx_note_appends_note_id_applied_at` on `(note_id, applied_at)`.

## `audit_logs`

Append-only security audit trail. Not vault-scoped — covers cross-vault operations like authentication. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1509-1559" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `uuid4()` | Primary key. |
| `timestamp` | timestamptz | no | `now()` | When the event occurred. |
| `actor` | varchar(255) | yes | NULL | API key identifier or `'anonymous'` when auth is disabled. |
| `action` | varchar(100) | no | — | Event type, e.g. `auth.success`, `auth.failure`, `note.create`. |
| `resource_type` | varchar(100) | yes | NULL | Type of affected resource (`note`, `entity`, `vault`, …). |
| `resource_id` | varchar(255) | yes | NULL | ID of the affected resource. |
| `session_id` | varchar(255) | yes | NULL | Request session ID for correlation. |
| `details` | jsonb | yes | NULL | Arbitrary event details (IP, user-agent, …). |

Indices:

- `idx_audit_logs_timestamp` on `(timestamp)`.
- `idx_audit_logs_actor` on `(actor)`.
- `idx_audit_logs_action` on `(action)`.
- `idx_audit_logs_resource` on `(resource_type, resource_id)`.

## `outcome_audit_log`

One row per `record_outcome` API call. Records the per-unit verb payload, coverage stats, and exploration tag so signal-quality regressions can be audited offline. Vault-scoped so outcome audit never leaks across tenants. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1562-1635" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | global vault UUID | FK → `vaults.id`, `ON DELETE CASCADE`. Indexed. |
| `caller_id` | varchar(128) | yes | NULL | Session id or caller fingerprint, no PII. |
| `units` | jsonb | no | — | Per-unit payload: list of `{unit_id, verb, reason?}`. |
| `turn_outcome` | text | yes | NULL | Coarse turn label: `success`, `failure`, `mixed`, or NULL. |
| `retrieved_set_size` | int | yes | NULL | Size of the retrieved set the caller classified. |
| `coverage_ratio` | float | yes | NULL | `reported / retrieved`; NULL when `retrieved_set_size` is unknown. |
| `exploration_tagged` | bool | no | `false` | True iff any unit was exploration-injected on retrieval. |
| `created_at` | timestamptz | no | `now()` | |

Indices:

- `idx_outcome_audit_log_vault_ts` on `(vault_id, created_at DESC)`.
- `idx_outcome_audit_log_caller` on `(caller_id)`.

CHECK constraints:

- `outcome_audit_log_units_is_array`: `jsonb_typeof(units) = 'array'`.

Application-level validator (Pydantic `@field_validator`): each element of `units` must be a `dict`; rows that fail validation raise before insertion.

JSON shape — `units[]`:

```json
{"unit_id": "9af3...", "verb": "helpful", "reason": "Fixed the rollback hook on first try."}
```

The `verb` is the Memory Worth signal — `helpful`, `not_helpful`, or `not_used`. A bare `success=True` payload returns HTTP 400.

## `maintenance_proposals`

Finding ledger row emitted by the `LintService`. Read-only from the agent surface. The unique partial index on `(rule_name, target_type, target_id, vault_id) WHERE status = 'pending'` makes `LintService.run_rules` idempotent on reruns. `vault_id` is nullable: NULL = global findings; reserved for Tier B. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1852-1951" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | yes | NULL | FK → `vaults.id`, `ON DELETE CASCADE`. NULL = global. |
| `lint_type` | text | no | — | `'structural'`, `'quality'`, `'governance'`, or `'schema'`. |
| `target_type` | text | no | — | Type of targeted entity (e.g. `'memory_unit'`, `'mental_model'`). |
| `target_id` | text | no | — | Opaque identifier of the target. |
| `rule_name` | text | no | — | Name of the rule that emitted this finding. |
| `evidence` | jsonb | no | `'{}'` | Rule-specific payload. |
| `suggested_action` | text | no | — | Free-text suggestion for agent or operator. |
| `status` | text | no | `'pending'` | `'pending'`, `'resolved'`, or `'dismissed'`. |
| `source` | text | no | `'rule'` | `'rule'` or `'llm'`. |
| `created_at` | timestamptz | no | `now()` | |
| `resolved_at` | timestamptz | yes | NULL | Set when `status` flips to resolved or dismissed. |
| `resolved_by` | text | yes | NULL | Free-text actor; NULL while pending. |

Indices:

- `idx_maintenance_proposals_vault_status` on `(vault_id, status)`.
- `idx_maintenance_proposals_lint_type` on `(lint_type)`.
- `uq_maintenance_proposals_pending` UNIQUE on `(rule_name, target_type, target_id, vault_id)` partial, `WHERE status = 'pending'`.

CHECK constraints:

- `ck_maintenance_proposals_lint_type`: `lint_type IN ('structural', 'quality', 'governance', 'schema')`.
- `ck_maintenance_proposals_status`: `status IN ('pending', 'resolved', 'dismissed')`.
- `ck_maintenance_proposals_source`: `source IN ('rule', 'llm')`.

## `consolidation_ticks`

One row per `consolidation_tick(vault_id)` invocation. `services/consolidation.py` is a thin orchestrator over reflection + contradiction + prune-stale; this row is its sole DB write at the end of each tick. `completed_at IS NULL` signals an in-progress tick; the gap between `started_at` and `completed_at` is wall-clock duration. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1954-2038" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | — | FK → `vaults.id`, `ON DELETE CASCADE`. NOT NULL (vault-scoping invariant). |
| `started_at` | timestamptz | no | — | Tick start. |
| `completed_at` | timestamptz | yes | NULL | Tick finish; NULL means in progress. |
| `units_processed` | int | no | `0` | Memory units returned by `select_diff_units` (capped at 500/tick). |
| `entities_reflected` | int | no | `0` | Distinct entities passed to `ReflectionService` during this tick. |
| `contradictions_run` | int | no | `0` | Contradiction-detection invocations. |
| `stale_pruned` | int | no | `0` | Units pruned by `prune_stale_evidence` (`status = STALE` only). |
| `error` | text | yes | NULL | Failure message; NULL on success. |
| `created_at` | timestamptz | no | `now()` | Row insertion. |

Indices:

- `idx_consolidation_ticks_vault_started` on `(vault_id, started_at)`.
- `idx_consolidation_ticks_vault_completed` on `(vault_id, completed_at DESC NULLS LAST)`.

## `lint_llm_quota`

Hour-bucket counter for the 24-hour rolling LLM-lint cost cap. One row per `(vault_id, hour_bucket)`. The rolling window is computed by summing the last 24 hour-buckets via the indexed range scan; UPSERT is idempotent through the unique constraint. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="2046-2087" />

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | UUID | no | `gen_random_uuid()` | Primary key. |
| `vault_id` | UUID | no | — | FK → `vaults.id`, `ON DELETE CASCADE`. |
| `hour_bucket` | timestamptz | no | — | UTC timestamp truncated to the hour. Clients must normalise. |
| `count` | int | no | `0` | LLM lint calls made in this hour for this vault. |

Indices / constraints:

- `uq_lint_llm_quota_vault_hour` UNIQUE on `(vault_id, hour_bucket)`.
- `idx_lint_llm_quota_vault_hour` on `(vault_id, hour_bucket)`.
- `ck_lint_llm_quota_count_non_negative`: `count >= 0`.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Configure Memex](../how-to/configuring-server/default-model.md)
- [Reference: Configuration](configuration.md)
- [Explanation: Hindsight Framework](../explanation/how-memex-works/high-level-architecture.md)
