# API routes

Every HTTP route the Memex Core server exposes today. All paths are prefixed `/api/v1` unless noted. The Prometheus endpoint at `/api/v1/metrics` is registered by `prometheus-fastapi-instrumentator` rather than a route handler — every other entry on this page maps to a Python handler cited inline.

The server is FastAPI. The OpenAPI document is served at `/openapi.json`; Swagger UI at `/docs`; ReDoc at `/redoc`. Those surfaces are not part of `/api/v1` and do not require API-key auth.

## Conventions

- **Auth.** When `server.auth.enabled` is true, requests must carry `X-API-Key`. Missing header returns 401; unknown key returns 403. Exempt paths (configurable via `server.auth.exempt_paths`) bypass the check; the defaults include `/api/v1/health`, `/api/v1/ready`, `/api/v1/metrics`, `/docs`, `/redoc`, `/openapi.json`. <code-ref path="packages/core/src/memex_core/server/auth.py" lines="123-184" />
- **Permission scopes.** Routes declare a scope through `Depends(require_read|require_write|require_delete)`. With auth disabled, every scope passes through. With auth enabled, a key's `policy` resolves to a frozenset of `Permission` values (`READ`, `WRITE`, `DELETE`). <code-ref path="packages/core/src/memex_core/server/auth.py" lines="197-220" />
- **Admin scope.** A second dependency, `require_admin_auth`, always requires a valid API key with `Policy.ADMIN`, even when global auth is disabled. <code-ref path="packages/core/src/memex_core/server/auth.py" lines="271-315" />
- **Per-vault scoping.** Routes that touch a vault call `check_vault_access`. A request that names a vault outside the key's `vault_ids` (write) or `vault_ids ∪ read_vault_ids` (read) returns 403. <code-ref path="packages/core/src/memex_core/server/auth.py" lines="223-268" />
- **Session header.** Every request may send `X-Session-ID`; the server generates one if absent and echoes it on the response. <code-ref path="packages/core/src/memex_core/server/__init__.py" lines="334-354" />
- **Rate limiting.** When `server.rate_limit.enabled` is true, requests are limited by client IP. Health, readiness, and metrics endpoints are exempt. Limit headers ride on the response.
- **Streaming.** List-shaped endpoints stream **newline-delimited JSON** (`application/x-ndjson`). One JSON object per line; an `{"error": "...", "type": "serialization_error"}` line may appear mid-stream on a malformed row. <code-ref path="packages/core/src/memex_core/server/common.py" lines="323-426" />
- **Error envelope.** Service-layer exceptions route through `_handle_error`: `VaultNotFoundError` / `ResourceNotFoundError` → 404; `AmbiguousResourceError` → 400; `AppendIdConflictError` / `NoteNotAppendableError` → 409; `AppendLockTimeoutError` → 503 with `Retry-After: 5`; `FeatureDisabledError` → 503; `DeltaValidationError` → 422; other `MemexError` → 400; unknown → 500 with `correlation_id`. <code-ref path="packages/core/src/memex_core/server/common.py" lines="55-99" />

## Index

| Method | Path | Auth scope | Summary |
|---|---|---|---|
| POST | `/api/v1/ingestions` | write | Ingest a Base64-encoded note artifact. |
| POST | `/api/v1/ingestions/url` | write | Scrape and ingest content from a URL. |
| POST | `/api/v1/ingestions/file` | write | Ingest a file from a path on the server. |
| POST | `/api/v1/ingestions/upload` | write | Multipart upload of one or more files. |
| POST | `/api/v1/ingestions/webhook` | write | Ingest a plain-JSON webhook payload. |
| POST | `/api/v1/ingestions/batch` | write | Start an asynchronous batch ingestion job. |
| GET | `/api/v1/ingestions/{job_id}` | write | Read the status of a batch job. |
| POST | `/api/v1/memories/search` | read | TEMPR memory-unit search (NDJSON). |
| POST | `/api/v1/memories/summary` | read | Synthesize a summary with citations from texts. |
| POST | `/api/v1/memories/by-chunks` | read | Resolve chunk IDs to memory units. |
| GET | `/api/v1/memories/{id}` | read | Get one memory unit. |
| DELETE | `/api/v1/memories/{id}` | delete | Delete a memory unit and its links. |
| POST | `/api/v1/memories/{id}/deprioritize` | write | Flip `is_deprioritized=true`. |
| POST | `/api/v1/memories/{id}/restore` | write | Flip `is_deprioritized=false`. |
| GET | `/api/v1/memories/{memory_id}/links` | read | Typed links from one memory unit. |
| GET | `/api/v1/memories/{memory_id}/history` | read | Supersession history tree. |
| POST | `/api/v1/memory/reconsolidate` | write | Re-run contradiction + reflection on one entity. |
| POST | `/api/v1/memory/consolidate` | write | Vault-wide low-MW consolidation pass. |
| POST | `/api/v1/memories/summarize-node` | write | Synchronous mental-model refresh on one entity. |
| POST | `/api/v1/survey` | read | Topic decomposition + grouped search. |
| GET | `/api/v1/notes` | read | List notes (NDJSON). |
| POST | `/api/v1/notes/search` | read | Note-level RRF search (NDJSON). |
| POST | `/api/v1/notes/related` | read | Notes related to the input via shared entities. |
| GET | `/api/v1/notes/find` | read | Fuzzy title search via trigrams. |
| GET | `/api/v1/notes/{note_id}` | read | Get a note by ID. |
| GET | `/api/v1/notes/{note_id}/page-index` | read | Hierarchical table of contents. |
| GET | `/api/v1/notes/{note_id}/metadata` | read | Metadata for one note. |
| POST | `/api/v1/notes/metadata/batch` | read | Metadata for many notes in one call. |
| GET | `/api/v1/notes/{note_id}/links` | read | Typed links from one note. |
| GET | `/api/v1/notes/{note_id}/memory_units` | read | Memory units belonging to a note. |
| GET | `/api/v1/notes/{id}/lineage` | read | Lineage tree for one note (deprecated alias). |
| POST | `/api/v1/notes/append` | write | Atomic, idempotent delta append. |
| POST | `/api/v1/notes/{note_id}/migrate` | write | Move a note to another vault. |
| PATCH | `/api/v1/notes/{note_id}/status` | write | Change lifecycle status. |
| PATCH | `/api/v1/notes/{note_id}/date` | write | Update publish date and cascade to units. |
| PATCH | `/api/v1/notes/{note_id}/title` | write | Rename a note. |
| PATCH | `/api/v1/notes/{note_id}/user-notes` | write | Replace user-notes and reprocess. |
| POST | `/api/v1/notes/{note_id}/assets` | write | Add assets via multipart upload. |
| DELETE | `/api/v1/notes/{note_id}/assets` | delete | Delete named assets from a note. |
| DELETE | `/api/v1/notes/{note_id}` | delete | Delete a note and its data. |
| GET | `/api/v1/nodes/{node_id}` | read | Get one page-index node. |
| POST | `/api/v1/nodes/batch` | read | Get many page-index nodes by ID. |
| GET | `/api/v1/entities` | read | List, search, or rank entities (NDJSON). |
| POST | `/api/v1/entities/batch` | read | Get many entities by ID. |
| GET | `/api/v1/entities/{id}` | read | Get one entity. |
| GET | `/api/v1/entities/{id}/mentions` | read | Memory units mentioning the entity (NDJSON). |
| GET | `/api/v1/entities/{id}/cooccurrences` | read | Cooccurrence edges for one entity (NDJSON). |
| GET | `/api/v1/cooccurrences` | read | Cooccurrences for many entities (NDJSON). |
| GET | `/api/v1/entities/{id}/lineage` | read | Entity lineage tree (deprecated alias). |
| POST | `/api/v1/entities/scan-merges` | write | One-shot cross-batch merge-cluster scan. |
| DELETE | `/api/v1/entities/{entity_id}` | delete | Delete an entity. |
| DELETE | `/api/v1/entities/{entity_id}/mental-model` | delete | Delete a mental model. |
| POST | `/api/v1/reflections` | write | Queue a reflection. |
| POST | `/api/v1/reflections/batch` | write | Queue many reflections (NDJSON). |
| GET | `/api/v1/reflections` | write | Inspect the reflection queue (NDJSON). |
| POST | `/api/v1/reflections/claim` | write | Atomically claim queue items (NDJSON). |
| GET | `/api/v1/admin/reflection/dlq` | delete | List dead-lettered reflection tasks. |
| POST | `/api/v1/admin/reflection/dlq/{item_id}/retry` | delete | Reset a dead-lettered item to pending. |
| GET | `/api/v1/vaults` | read | List vaults (NDJSON). |
| POST | `/api/v1/vaults` | write | Create a vault. |
| GET | `/api/v1/vaults/{identifier}` | read | Get or resolve a vault by ID or name. |
| DELETE | `/api/v1/vaults/{vault_id}` | delete | Delete a vault. |
| POST | `/api/v1/vaults/{vault_id}/truncate` | delete | Empty a vault without deleting it. |
| POST | `/api/v1/vaults/{identifier}/set-writer` | write | Set active write vault (runtime override). |
| POST | `/api/v1/vaults/{identifier}/set-reader` | write | Set default read vault (runtime override). |
| GET | `/api/v1/vaults/{vault_id}/summary` | read | Get the cached vault summary. |
| POST | `/api/v1/vaults/{vault_id}/summary/regenerate` | write | Rebuild the vault summary. |
| GET | `/api/v1/vaults/{vault_id}/session-briefing` | read | Token-budgeted briefing for LLM agents. |
| GET | `/api/v1/lineage/{entity_type}/{id}` | read | Unified lineage tree for any entity type. |
| GET | `/api/v1/resources/{path}` | read | Stream a raw filestore resource. |
| POST | `/api/v1/embed` | read | Embed a text string. |
| PUT | `/api/v1/kv` | write | Upsert a KV entry. |
| GET | `/api/v1/kv` | read | List KV entries. |
| GET | `/api/v1/kv/get` | read | Get a KV entry by key. |
| POST | `/api/v1/kv/search` | read | Semantic KV search. |
| DELETE | `/api/v1/kv/delete` | delete | Delete a KV entry. |
| POST | `/api/v1/outcomes/record` | write | Record per-unit outcome verbs. |
| GET | `/api/v1/lint/status` | read | Pending lint-finding counts. |
| GET | `/api/v1/lint/findings` | read | List lint findings (offset paging). |
| GET | `/api/v1/lint/flags` | read | Cursor-paginated agent surface for findings. |
| POST | `/api/v1/lint/findings/{finding_id}/dismiss` | write | Mark a finding dismissed. |
| POST | `/api/v1/lint/findings/{finding_id}/resolve` | write | Mark a finding resolved (rule-keyed dispatcher). |
| POST | `/api/v1/lint/findings/{finding_id}/apply` | write | Apply a winner-proposal action. |
| POST | `/api/v1/lint/findings/{finding_id}/reverse` | write | Reverse a previously applied winner-proposal. |
| POST | `/api/v1/lint/run/{vault_id}` | write | Synchronously run V1 lint rules. |
| POST | `/api/v1/lint/llm/run/{vault_id}` | write | Synchronously run LLM-gated lint checks. |
| POST | `/api/v1/consolidation/tick` | write | Run one or more consolidation ticks. |
| GET | `/api/v1/consolidation/status` | read | Last-run timestamps per vault. |
| GET | `/api/v1/diagnostics/manifold/{vault_id}` | read | UMAP projection (warm cache or 202 task ID). |
| GET | `/api/v1/diagnostics/manifold/{vault_id}/status` | read | Poll a manifold compute task. |
| GET | `/api/v1/diagnostics/retrieval/{vault_id}` | read | Top-N outcome heatmap. |
| GET | `/api/v1/diagnostics/summary/{vault_id}` | read | Diagnostics summary blob. |
| GET | `/api/v1/diagnostics/lint/{vault_id}` | read | Lint-dashboard pivot. |
| GET | `/api/v1/admin/audit` | admin | Query the audit log. |
| GET | `/api/v1/system/config` | admin | Resolved server config with secrets redacted. |
| GET | `/api/v1/stats/counts` | read | System-wide counts. |
| GET | `/api/v1/health` | none | Liveness probe. |
| GET | `/api/v1/ready` | none | Readiness probe (DB + filestore + tracing). |
| GET | `/api/v1/metrics` | none | Prometheus metrics (instrumentator-registered). |

---

## Ingestion

### POST /api/v1/ingestions

Ingest a Base64-encoded note. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="146-228" />

- **Auth.** `require_write`. The router declares `Depends(require_write)` at registration; every `/ingestions/*` route inherits it. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="63" />
- **Body** (`NoteCreateDTO`): `name`, `description`, `content` (Base64-encoded UTF-8), optional `files` (Base64-encoded map), optional `tags`, `note_key`, `vault_id`, `user_notes`, `author`, `template`, `filename`, `event_date`, `intent_class`, `risk_class`.
- **Query params.** `background` (bool, default `false`).
- **Returns.** `IngestResponse` (200) — `{note_id, unit_ids, status, reason}`. With `background=true`, `BatchJobStatus` (202).
- **Errors.** 400 invalid Base64; 409 `OverlapError` (in-flight job shares idempotency keys; carries `Location: /api/v1/ingestions/<existing-id>`); other `MemexError` → 400; unhandled → 500.

### POST /api/v1/ingestions/url

Scrape a URL and ingest the result. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="231-281" />

- **Auth.** `require_write`.
- **Body** (`IngestURLRequest`): `url`, optional `vault_id`, `reflect_after`, `assets` (Base64 map), `user_notes`.
- **Query params.** `background` (bool, default `false`).
- **Returns.** `IngestResponse` (200) or `BatchJobStatus` (202).
- **Errors.** Same envelope as `/ingestions`; 409 on overlap.

### POST /api/v1/ingestions/file

Ingest a file already present on the server's filesystem. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="284-300" />

- **Auth.** `require_write`.
- **Body** (`IngestFileRequest`): `file_path` (absolute), optional `vault_id`, `reflect_after`, `user_notes`.
- **Returns.** `IngestResponse`.
- **Errors.** Standard envelope.

### POST /api/v1/ingestions/upload

Multipart upload of one or more files. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="303-456" />

- **Auth.** `require_write`.
- **Content type.** `multipart/form-data`.
- **Form fields.**
  - `files`: one or more files.
  - `metadata`: optional JSON string carrying `name`, `description`, `tags`, `vault_id`, `note_key`, `user_notes`.
- **Query params.** `background` (bool, default `false`).
- **Behaviour.** A single non-markdown file goes through MarkItDown conversion. With multiple files the main markdown file is picked by priority — `NOTE.md` → `README.md` → `index.md` → first `*.md`. Non-priority files become assets.
- **Returns.** `IngestResponse` (200) or `BatchJobStatus` (202).
- **Errors.** 400 when no main file can be identified.

### POST /api/v1/ingestions/webhook

Plain-JSON ingestion for external webhooks. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="471-546" />

- **Auth.** `require_write` plus an optional HMAC layer. When `server.auth.webhook_secret` is set, the request must carry header `X-Webhook-Signature: hex(HMAC-SHA256(secret, raw_body))`. Missing → 401; wrong → 403.
- **Body** (`WebhookPayload`): `title`, `content` (plain UTF-8), `source`, optional `description`, `tags`, `vault_id`.
- **Idempotency.** The server derives `note_key = "webhook:<source>:<sha256(source:content)>"`.
- **Returns.** Always `BatchJobStatus` with HTTP 202.
- **Errors.** 400 invalid payload, 401/403 signature, 409 overlap.

### POST /api/v1/ingestions/batch

Submit a batch of notes for asynchronous ingestion. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="549-584" />

- **Auth.** `require_write`.
- **Body** (`BatchIngestRequest`): `notes` (`NoteCreateDTO[]`, min length 1), optional `vault_id`, `batch_size`.
- **Returns.** `BatchJobStatus` with HTTP 202.
- **Errors.** 409 overlap (with `Location`); 422 empty `notes`; other `MemexError` → 400.

### GET /api/v1/ingestions/{job_id}

Read the status of a batch job. <code-ref path="packages/core/src/memex_core/server/ingestion.py" lines="587-614" />

- **Auth.** `require_write` (inherited from the `/ingestions/*` router).
- **Path params.** `job_id` (UUID).
- **Returns.** `BatchJobStatus`: `{job_id, status: pending|processing|completed|failed, progress, processed_count, total_count, result?}`. `result` is `BatchIngestResponse` when `status == completed`.
- **Errors.** 404 if the job ID is unknown.

---

## Search

### POST /api/v1/memories/search

TEMPR memory-unit search across one or more vaults. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/retrieval.py" lines="28-85" />

- **Auth.** `require_read`. The retrieval router declares the dependency at registration. <code-ref path="packages/core/src/memex_core/server/retrieval.py" lines="25" />
- **Body** (`RetrievalRequest`): `query`, optional `limit`, `vault_ids`, `token_budget`, `strategies` (`semantic|keyword|graph|temporal|mental_model`), `include_stale`, `include_superseded`, `include_deprioritized`, `apply_pre_filter`, `debug`, `after`, `before`, `tags`, `source_context`, `reference_date`, `expand_query`, `intent_class`, `risk_class`.
- **Returns.** NDJSON; each line is `MemoryUnitDTO`. With `debug=true`, each unit carries `debug_info` with per-strategy attribution.
- **Errors.** 403 cross-vault access denied; standard envelope otherwise.

### POST /api/v1/memories/summary

Synthesize a summary with citations from caller-supplied texts. <code-ref path="packages/core/src/memex_core/server/summary.py" lines="19-37" />

- **Auth.** `require_read`.
- **Body** (`SummaryRequest`): `query`, `texts` (string list, usually the texts of search results).
- **Returns.** `SummaryResponse` — `{summary}`.
- **Errors.** Standard envelope; LLM-call failures bubble up as 400 `MemexError`.

### POST /api/v1/notes/search

Note-level search with multi-channel fusion. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="153-185" />

- **Auth.** `require_read`.
- **Body** (`NoteSearchRequest`): `query`, optional `limit` (default 5), `vault_ids`, `expand_query`, `fusion_strategy` (`rrf|position_aware`), `strategies` (`semantic|keyword|graph|temporal`), `strategy_weights`, `reason`, `summarize`, `mmr_lambda`, `after`, `before`, `tags`, `reference_date`.
- **Returns.** NDJSON; each line is `NoteSearchResult` with `note_id`, `score`, `snippets`, `metadata`, `vault_id`, `vault_name`, `note_status`, `reasoning`, `answer`, `related_notes`, `links`.
- **Errors.** 403 cross-vault; standard envelope otherwise.

### POST /api/v1/survey

Decompose a broad topic into sub-questions, then run parallel searches and group the results. <code-ref path="packages/core/src/memex_core/server/survey.py" lines="20-40" />

- **Auth.** `require_read`.
- **Body** (`SurveyRequest`): `query`, optional `vault_ids`, `limit_per_query` (default 10), `token_budget`, `after`, `before`, `reference_date`.
- **Returns.** `SurveyResponse` — `{groups: [...]}`, grouped by source note.
- **Errors.** 403 cross-vault; standard envelope otherwise.

### GET /api/v1/notes/find

Fuzzy-search notes by title via trigram similarity. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="205-228" />

- **Auth.** `require_read`.
- **Query params.** `query` (required), `vault_id` (repeat for many), `limit` (1-500, default 5), `threshold` (0.0-1.0, default 0.3).
- **Returns.** `FindNoteResult[]` — each entry carries `note_id`, `title`, `score`, `vault_id`, `created_at`, optional `publish_date`, `status`.

---

## Entities

### GET /api/v1/entities

List, search, or rank entities. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="42-108" />

- **Auth.** `require_read`.
- **Query params.** `limit` (1-500, default 100), `query` (name search), `q` (deprecated alias for `query`), `sort` (`-mentions` only), `vault_id` (repeat), `entity_type` (`Person|Organization|Location|Concept|Technology|File|Misc`), `slim` (bool — drop entity description to fit hook caps).
- **Returns.** NDJSON; each line is `EntityDTO`.
- **Behaviour.** With `query`, runs entity search. With `sort=-mentions`, returns top entities. Otherwise streams entities ranked by relevance.

### POST /api/v1/entities/batch

Get many entities by ID. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="183-199" />

- **Auth.** `require_read`.
- **Body.** `{entity_ids: UUID[]}`.
- **Query params.** `vault_id` (scopes the lookup; defaults to the active vault).
- **Returns.** `EntityDTO[]`.

### GET /api/v1/entities/{id}

Get one entity. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="202-218" />

- **Auth.** `require_read`.
- **Path params.** `id` (UUID).
- **Query params.** `vault_id`.
- **Returns.** `EntityDTO`.
- **Errors.** 404 unknown entity.

### GET /api/v1/entities/{id}/mentions

Memory units mentioning the entity. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="141-176" />

- **Auth.** `require_read`.
- **Path params.** `id` (UUID).
- **Query params.** `limit` (1-500, default 20), `vault_id` (repeat), `include_stale`, `include_superseded`, `include_deprioritized`.
- **Returns.** NDJSON; each line is `{unit: MemoryUnitDTO, note: NoteDTO}`.

### GET /api/v1/entities/{id}/cooccurrences

Cooccurrence edges originating from one entity. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="221-252" />

- **Auth.** `require_read`.
- **Path params.** `id` (UUID).
- **Query params.** `limit` (1-500, default 50), `vault_id` (repeat).
- **Returns.** NDJSON. Each line carries `entity_id_1`, `entity_id_2`, `entity_1_name`, `entity_1_type`, `entity_2_name`, `entity_2_type`, `cooccurrence_count`, `vault_id`.

### GET /api/v1/cooccurrences

Cooccurrences for many entities in one call. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="111-138" />

- **Auth.** `require_read`.
- **Query params.** `ids` (comma-separated UUIDs, required), `vault_id` (repeat).
- **Returns.** NDJSON. Each line: `entity_id_1`, `entity_id_2`, `cooccurrence_count`, `vault_id`.

### GET /api/v1/entities/{id}/lineage

Deprecated alias for `GET /api/v1/lineage/{entity_type}/{id}`. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="256-289" />

- **Auth.** `require_read`.
- **Path params.** `id` (UUID).
- **Query params.** `direction` (`upstream|downstream`, default `upstream`), `depth` (1-10, default 3), `limit` (1-500, default 10).
- **Response headers.** `Deprecation: true`, `Sunset: 2026-06-01`, `Link: </api/v1/lineage>; rel="successor-version"`.
- **Returns.** `LineageResponse`.

### POST /api/v1/entities/scan-merges

Run a one-shot cluster-collapse scan. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="302-336" />

- **Auth.** `require_write`. Cross-vault — operator-only in deployments that enable auth.
- **Query params.** `top_n` (2-10000, optional), `scan_cooldown_days` (≥0, optional), `pair_threshold` (0.0-1.0, optional), `cluster_min_threshold` (0.0-1.0, optional). All four default to config values.
- **Behaviour.** Inserts/updates `maintenance_proposals` and bumps `entities.last_merge_scan_at`. The collapse itself is not applied — operators approve via the lint resolve flow.
- **Returns.** A scan-summary dict.

### DELETE /api/v1/entities/{entity_id}

Delete an entity and everything anchored to it. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="292-299" />

- **Auth.** `require_delete`.
- **Path params.** `entity_id` (UUID).
- **Returns.** `{status: "success"}`.

### DELETE /api/v1/entities/{entity_id}/mental-model

Delete a mental model for one entity in one vault. <code-ref path="packages/core/src/memex_core/server/entities.py" lines="339-353" />

- **Auth.** `require_delete`.
- **Path params.** `entity_id` (UUID).
- **Query params.** `vault_id` (defaults to active vault).
- **Returns.** `{status: "success"}`.

---

## Notes

### GET /api/v1/notes

List notes. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="54-150" />

- **Auth.** `require_read`.
- **Query params.** `limit` (1-500, default 100), `offset` (≥0, default 0), `sort` (`-created_at` only), `vault_id` (repeat), `after` (ISO 8601), `before` (ISO 8601), `template` (slug), `tags` (repeat, AND semantics), `status`, `date_field` (`coalesce|created_at|publish_date`, default `coalesce`), `slim` (bool).
- **Returns.** NDJSON; each line is `NoteListItemDTO`.
- **Errors.** 400 invalid `after`/`before` ISO string.

### POST /api/v1/notes/related

Notes related via shared entities. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="192-202" />

- **Auth.** `require_read`.
- **Body.** `{note_ids: UUID[]}`.
- **Returns.** A dict keyed by input note ID; each value is a list of up to 5 related notes with `note_id`, `title`, `shared_entities`, `strength`.

### GET /api/v1/notes/{note_id}

Get one note. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="251-258" />

- **Auth.** `require_read`.
- **Path params.** `note_id` (UUID).
- **Returns.** `NoteDTO`.

### GET /api/v1/notes/{note_id}/page-index

Hierarchical table of contents. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="231-238" />

- **Auth.** `require_read`.
- **Path params.** `note_id` (UUID).
- **Returns.** `{note_id, page_index: [...]}`. Each tree node carries `node_id`, `title`, `level`, `token_estimate`, `summary`, `children`. Use the `node_id`s with `GET /nodes/{node_id}`.

### GET /api/v1/notes/{note_id}/metadata

Note metadata (title, tags, token count, assets, publish date). <code-ref path="packages/core/src/memex_core/server/notes.py" lines="241-248" />

- **Auth.** `require_read`.
- **Path params.** `note_id` (UUID).
- **Returns.** `{note_id, metadata}`.

### POST /api/v1/notes/metadata/batch

Metadata for many notes. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="281-290" />

- **Auth.** `require_read`.
- **Body.** `{note_ids: UUID[]}`.
- **Returns.** A list of metadata objects.

### GET /api/v1/notes/{note_id}/links

Typed links from one note. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="538-555" />

- **Auth.** `require_read`.
- **Path params.** `note_id` (UUID).
- **Query params.** `link_type` (e.g. `contradicts`), `limit` (1-200, default 20).
- **Returns.** `MemoryLinkDTO[]` aggregated from the note's memory units.

### GET /api/v1/notes/{note_id}/memory_units

Memory units belonging to a note. Used by the eval suite to map note keys back to unit IDs. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="91-123" />

- **Auth.** `require_read`.
- **Path params.** `note_id` (UUID).
- **Query params.** `vault_id` (UUID, **required**). A mismatch returns 403.
- **Returns.** `MemoryUnitDTO[]`.

### GET /api/v1/notes/{id}/lineage

Deprecated alias for `GET /api/v1/lineage/note/{id}`. <code-ref path="packages/core/src/memex_core/server/resources.py" lines="43-68" />

- **Auth.** `require_read`.
- **Query params.** `direction` (`upstream|downstream`, default `upstream`), `depth` (1-10, default 3), `limit` (1-500, default 10).
- **Response headers.** `Deprecation: true`, `Sunset: 2026-06-01`, `Link: </api/v1/lineage>; rel="successor-version"`.
- **Returns.** `LineageResponse`.

### POST /api/v1/notes/append

Atomic, idempotent delta append. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="395-449" />

- **Auth.** `require_write`. Vault scope is gated against the parent's resolved vault.
- **Body** (`NoteAppendRequest`):
  - One of (`note_key` with `vault_id`) **or** `note_id`.
  - `delta` (1-200000 UTF-8 bytes; must not start with `---\n`, be whitespace-only, or contain NUL).
  - `append_id` (UUID, caller-supplied — required; reuse returns `status="replayed"`).
  - `joiner` (`paragraph`/`newline`/`none`, default `paragraph`).
  - Optional `user_notes`.
- **Returns.** `NoteAppendResponse` — `{status, note_id, append_id, content_hash, delta_bytes, new_unit_ids}`. `status` is `success` or `replayed`.
- **Errors.**
  - 404 parent not found, or `note_key` resolves to a different vault than `vault_id`.
  - 409 parent not appendable (`archived`/`superseded`), or `append_id` reused with different parent/delta/joiner.
  - 422 missing identifier, both `note_id` and `note_key` supplied, oversized delta, frontmatter delta, NUL bytes.
  - 503 append lock not acquired within 30 s (transient — `Retry-After: 5`), or feature disabled (`server.append_enabled=false`, no `Retry-After`).

### POST /api/v1/notes/{note_id}/migrate

Move a note and its data to another vault. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="469-480" />

- **Auth.** `require_write`.
- **Path params.** `note_id` (UUID).
- **Body.** `{target_vault_id: string}`.
- **Returns.** A migration summary.

### PATCH /api/v1/notes/{note_id}/status

Change lifecycle status. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="312-322" />

- **Auth.** `require_write`.
- **Path params.** `note_id` (UUID).
- **Body.** `{status: "active"|"superseded", linked_note_id?: UUID}`.
- **Behaviour.** `superseded` marks every memory unit stale. To archive, call the archive endpoint instead.
- **Returns.** The updated status payload.

### PATCH /api/v1/notes/{note_id}/date

Update the publish date and cascade the delta onto memory unit timestamps. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="329-346" />

- **Auth.** `require_write`.
- **Path params.** `note_id` (UUID).
- **Body.** `{date: ISO 8601 string}`.
- **Errors.** 400 invalid date string.

### PATCH /api/v1/notes/{note_id}/title

Rename a note. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="353-364" />

- **Auth.** `require_write`.
- **Path params.** `note_id` (UUID).
- **Body.** `{new_title: string}`.
- **Returns.** `{status: "success", note_id, new_title}`.

### PATCH /api/v1/notes/{note_id}/user-notes

Replace user notes and reprocess them into the memory graph. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="525-535" />

- **Auth.** `require_write`.
- **Path params.** `note_id` (UUID).
- **Body.** `{user_notes: string | null}`. `null` deletes all user annotations.
- **Returns.** `{units_deleted, units_created}`.

### POST /api/v1/notes/{note_id}/assets

Add assets via multipart upload. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="483-500" />

- **Auth.** `require_write`.
- **Content type.** `multipart/form-data` with one or more `files`.
- **Returns.** `{added_assets, skipped, asset_count}`.

### DELETE /api/v1/notes/{note_id}/assets

Delete one or more asset files. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="507-518" />

- **Auth.** `require_delete`.
- **Path params.** `note_id` (UUID).
- **Body.** `{asset_paths: string[]}`.
- **Returns.** `{deleted_assets, not_found, asset_count}`.

### DELETE /api/v1/notes/{note_id}

Delete the note and all its data. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="452-462" />

- **Auth.** `require_delete`.
- **Path params.** `note_id` (UUID).
- **Returns.** `{status: "success"}`.

### GET /api/v1/nodes/{node_id}

Get one page-index node. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="293-304" />

- **Auth.** `require_read`.
- **Path params.** `node_id` (UUID).
- **Returns.** `NodeDTO` — `id`, `note_id`, `title`, `level`, `seq`, `status`, `text`.
- **Errors.** 404 unknown node.

### POST /api/v1/nodes/batch

Get many page-index nodes by ID. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="265-274" />

- **Auth.** `require_read`.
- **Body.** `{node_ids: UUID[]}`.
- **Returns.** `NodeDTO[]`.

---

## Memory

### GET /api/v1/memories/{id}

Get one memory unit. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="78-88" />

- **Auth.** `require_read`.
- **Path params.** `id` (UUID).
- **Returns.** `MemoryUnitDTO`.
- **Errors.** 404 not found.

### DELETE /api/v1/memories/{id}

Delete a memory unit. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="126-133" />

- **Auth.** `require_delete`.
- **Path params.** `id` (UUID).
- **Returns.** `{status: "success"}`.

### POST /api/v1/memories/by-chunks

Resolve chunk IDs to memory units, vault-scoped. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="54-75" />

- **Auth.** `require_read`. The route checks vault access against the supplied `vault_id`.
- **Body.** `{chunk_ids: UUID[], vault_id: UUID}` — both required. Cross-vault returns 403.
- **Returns.** `MemoryUnitDTO[]`.

### POST /api/v1/memories/{id}/deprioritize

Flip `is_deprioritized=true` on a memory unit. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="172-211" />

- **Auth.** `require_write` + vault access against `request.vault_id`.
- **Path params.** `id` (UUID).
- **Body.** `{reason: string, vault_id: UUID, actor?: string}`.
- **Errors.**
  - 400 the target is a virtual observation (read-only projection). The body carries `source_memory_units`; re-issue against one of those IDs.
  - 404 memory unit not found.

### POST /api/v1/memories/{id}/restore

Flip `is_deprioritized=false`. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="214-239" />

- **Auth.** `require_write` + vault access.
- **Path params.** `id` (UUID).
- **Body.** `{vault_id: UUID, actor?: string}`.
- **Errors.** 404 memory unit not found.

### POST /api/v1/memory/reconsolidate

Re-run contradiction detection and reflection on one entity under a per-entity advisory lock. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="324-395" />

- **Auth.** `require_write` + vault access.
- **Body.** `{entity_id: UUID, vault_id: UUID, timeout_seconds: float (0.1-300.0, default 30.0)}`.
- **Returns.** `ReconsolidateResponse` — `entity_id`, `vault_id`, `units_examined`, `contradictions_run`, optional `mental_model_id`, `observations_added`, `abandoned`, optional `error`.
- **Errors.** 503 lock-acquisition timeout, with `Retry-After` derived from `timeout_seconds`.

### POST /api/v1/memory/consolidate

Vault-wide low-Memory-Worth consolidation. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="398-469" />

- **Auth.** `require_write` + vault access.
- **Body.** `{vault_id: UUID, dry_run: bool (default false), actor?: string}`.
- **Behaviour.** Predicate: `mw_score < 0.35 AND outcomes >= 5 AND !is_deprioritized AND created_at < now() - 30d`. `dry_run=true` returns a preview without writes.
- **Errors.**
  - 429 rate limit (default 1 call per vault per hour). Body carries `error`, `retry_after_seconds`, `message`. `Retry-After` header set.
  - 503 lock timeout, with `Retry-After`.

### POST /api/v1/memories/summarize-node

Synchronous mental-model refresh on one entity. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="80-192" />

- **Auth.** `require_write`. (Vault-access check happens at the service layer.)
- **Body.** `{entity_id: UUID, scope: "incremental"|"full" (default "incremental"), vault_id?: UUID (defaults to global vault)}`.
- **Returns.** `ReflectionResultDTO` — `entity_id`, `new_observations`, `status="completed"`.
- **Errors.**
  - 429 rate-limit exceeded — body `{error, retry_after_seconds, message}`, `Retry-After` header.
  - 503 `reflection_abandoned` — a concurrent worker refreshed first; body `{error, retry_after_seconds, message, hint}`, `Retry-After` header.

### GET /api/v1/memories/{memory_id}/links

Typed relationship links for one memory unit. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="472-490" />

- **Auth.** `require_read`.
- **Path params.** `memory_id` (UUID).
- **Query params.** `link_type`, `limit` (1-200, default 20).
- **Returns.** `MemoryLinkDTO[]`.

### GET /api/v1/memories/{memory_id}/history

Walk the contradiction graph backward to produce the supersession history tree. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="493-533" />

- **Auth.** `require_read` + vault access.
- **Path params.** `memory_id` (UUID).
- **Query params.** `vault_id` (UUID, **required**), `max_depth` (0-50, default 10).
- **Returns.** `UnitHistoryNodeDTO` — ordered tree rooted at the queried unit (`depth=0`). `reinforces` links are excluded; v1 is supersession-only.
- **Errors.** 404 memory unit not found.

---

## KV

### PUT /api/v1/kv

Upsert a KV entry. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="49-64" />

- **Auth.** `require_write`.
- **Body** (`KVPutRequest`): `key`, `value`, optional `embedding`, `ttl_seconds`. If `embedding` is omitted the server generates one.
- **Returns.** `KVEntryDTO`.

### GET /api/v1/kv

List KV entries. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="153-186" />

- **Auth.** `require_read`.
- **Query params.** `limit` (1-500, default 100), `namespaces` (comma-separated prefixes), `exclude_prefix`, `key_prefix`, `pattern` (trailing `*` only).
- **Returns.** `KVEntryDTO[]`.

### GET /api/v1/kv/get

Get a KV entry by exact key. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="67-107" />

- **Auth.** `require_read`.
- **Query params.** `key` (required), `include_history` (bool, default `false`).
- **Returns.** `KVEntryDTO`. For `procedure:` keys with `include_history=true`, the server returns `KVProcedureEntryDTO` carrying the active value plus version + history.
- **Errors.** 404 unknown key.

### POST /api/v1/kv/search

Semantic search over KV entries. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="110-133" />

- **Auth.** `require_read`.
- **Body** (`KVSearchRequest`): exactly one of `query` (text — the server embeds it) or `query_embedding` (pre-computed vector); optional `namespaces` (list of prefixes), `limit` (1-500, default 5).
- **Returns.** `KVEntryDTO[]` ranked by embedding similarity.

### DELETE /api/v1/kv/delete

Delete a KV entry. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="136-150" />

- **Auth.** `require_delete`.
- **Query params.** `key` (required).
- **Returns.** `{status: "success"}`.
- **Errors.** 404 unknown key.

---

## Outcomes

### POST /api/v1/outcomes/record

Record per-unit outcome verbs against memory units that were used in a turn. <code-ref path="packages/core/src/memex_core/server/outcomes.py" lines="110-158" />

- **Auth.** `require_write`. When `vault_id` is supplied, the route checks vault access.
- **Body** (`RecordOutcomeRequest`, `extra="forbid"`):
  - Preferred shape: `units: [{unit_id, verb: "helpful"|"not_helpful"|"not_used", reason?}]`.
  - Legacy shape (still accepted; emits `FutureWarning`): `success: bool` + `unit_ids: string[]`.
  - Optional: `vault_id` (UUID or name), `outcome_confidence` (0.0-1.0, default 1.0), `reason`, `caller_id`, `turn_outcome`, `retrieved_set_size`, `exploration_tagged`.
- **Returns.** A service-layer summary dict.
- **Errors.**
  - 400 unknown `vault_id`, missing `unit_ids`/`vault_id`, malformed payload.
  - 422 unknown body fields (legacy `target_type` / `kv_key`).

---

## Lint

### GET /api/v1/lint/status

Pending finding counts. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="72-106" />

- **Auth.** `require_read`. With `scope=vault`, the route also checks vault access.
- **Query params.** `vault_id` (UUID — required when `scope=vault`), `scope` (`all|vault|global`, default `all`).
- **Returns.**
  - `scope=all`: `{scope: "all", pending: <int>}`.
  - `scope=vault`: `{scope: "vault", vault_id, pending}`.
  - `scope=global`: `{scope: "global", pending}`.

### GET /api/v1/lint/findings

List lint findings (offset paging — CLI surface). <code-ref path="packages/core/src/memex_core/server/lint.py" lines="109-158" />

- **Auth.** `require_read` + vault access on `vault_id`.
- **Query params.** `vault_id`, `lint_type` (`structural|quality|governance|schema`), `status` (`pending|resolved|dismissed`, default `pending`), `limit` (1-500, default 50), `offset` (≥0, default 0).
- **Returns.** `{count, findings: [...]}` — each finding carries `id`, `vault_id`, `lint_type`, `target_type`, `target_id`, `rule_name`, `evidence`, `suggested_action`, `status`, `source`, `created_at`, `resolved_at`, `resolved_by`, `target_text`.

### GET /api/v1/lint/flags

Cursor-paginated agent surface — shape-stable across pages. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="646-694" />

- **Auth.** `require_read` + vault access on `vault_id`.
- **Query params.** `vault_id`, `lint_type`, `target_type`, `status` (default `pending`), `limit` (1-200, default 20), `cursor` (opaque).
- **Returns.** `{findings: [...], next_cursor: string | null}`.
- **Errors.** 503 `{error: "lint_subsystem_not_initialized", message, missing_migration: "025_maintenance_proposals"}` if the maintenance ledger schema is absent; 400 on bad cursor.

### POST /api/v1/lint/findings/{finding_id}/dismiss

Flip a pending finding to `dismissed`. Idempotent. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="207-226" />

- **Auth.** `require_write` + vault access on the finding's own vault (looked up first, so a leaked finding ID from another vault returns 403).
- **Path params.** `finding_id` (UUID).
- **Returns.** `{finding_id, status: "dismissed"}`.
- **Errors.** 404 unknown finding or not pending.

### POST /api/v1/lint/findings/{finding_id}/resolve

Flip a pending finding to `resolved`. Rule-keyed. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="229-261" />

- **Auth.** `require_write` + vault access (per-finding for ordinary rules; per cluster member for `entity_collapse_cluster`).
- **Path params.** `finding_id` (UUID).
- **Body.** Optional dict. For `entity_collapse_cluster` findings, must include `winner_id` (UUID) **or** `winner_canonical_name`.
- **Returns.** For ordinary findings: `{finding_id, status: "resolved"}`. For collapse clusters: `{finding_id, status: "resolved", rule_name: "entity_collapse_cluster", winner_id, winner_overridden, summary}`.
- **Errors.**
  - 400 ambiguous winner, non-member winner, missing `vaults_affected`, no losers.
  - 404 unknown finding or not pending.
  - 409 finding state changed during apply.

### POST /api/v1/lint/findings/{finding_id}/apply

Apply a winner-proposal finding's recorded action. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="421-448" />

- **Auth.** `require_write` + vault access on the finding's vault. Refused with 403 when `server.auth.enabled=false` unless the operator sets `MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1|true|yes`.
- **Path params.** `finding_id` (UUID).
- **Returns.** A dispatcher result from `apply_winner_proposal`.
- **Errors.** 409 conflicting application state.

### POST /api/v1/lint/findings/{finding_id}/reverse

Reverse a previously applied winner-proposal by reading `evidence.resolution.prior_state` and atomically restoring rows. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="451-477" />

- **Auth.** Same as `apply` (including the `MEMEX_LINT_ALLOW_UNATTENDED_APPLY` gate).
- **Path params.** `finding_id` (UUID).
- **Returns.** A reversal summary dict.
- **Errors.** 409 conflicting reversal state.

### POST /api/v1/lint/run/{vault_id}

Synchronously run the V1 lint rule registry for one vault. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="480-524" />

- **Auth.** `require_write` + vault access.
- **Path params.** `vault_id` (UUID).
- **Returns.** `{vault_id, total_findings, rules: [{name, lint_type, findings_emitted, duration_seconds, error}]}`.
- **Errors.** 503 lint subsystem not initialized.

### POST /api/v1/lint/llm/run/{vault_id}

Synchronously run LLM-gated lint checks (semantic contradiction, schema drift, propose-winner) for one vault. <code-ref path="packages/core/src/memex_core/server/lint.py" lines="527-643" />

- **Auth.** `require_write` + vault access.
- **Path params.** `vault_id` (UUID).
- **Returns.** `{vault_id, summaries: [{check, evaluated, emitted, deferred, deferred_processed} | {check, error}]}`. With every check disabled the response is `{vault_id, summaries: [], detail: "no LLM lint checks enabled"}`.
- **Errors.** 503 when `server.memory.lint_llm.enabled=false` or `cost_cap_per_24h=0`.

---

## Vaults

### GET /api/v1/vaults

List vaults. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="76-186" />

- **Auth.** `require_read`.
- **Query params.** `state` (`active` only), `is_default` (bool).
- **Returns.** NDJSON; each line is `VaultDTO` carrying `id`, `name`, `description`, `mw_mode`, `is_active`, `note_count`, `last_note_added_at`, `access`.
- **Behaviour.** Default streams every vault with counts. `state=active` returns just the active vault. `is_default=true` returns the active vault plus the default reader vault (when distinct).

### POST /api/v1/vaults

Create a vault. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="189-200" />

- **Auth.** `require_write`.
- **Body** (`CreateVaultRequest`): `name`, optional `description`.
- **Returns.** `VaultDTO`.

### GET /api/v1/vaults/{identifier}

Get a vault by UUID or resolve a vault name to its UUID. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="203-229" />

- **Auth.** `require_read`.
- **Path params.** `identifier` (UUID string or name).
- **Returns.** `{id: <UUID>}`.
- **Errors.** 404 unknown vault.

### DELETE /api/v1/vaults/{vault_id}

Delete a vault. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="232-241" />

- **Auth.** `require_delete`.
- **Path params.** `vault_id` (UUID).
- **Returns.** `{status: "success"}`.
- **Errors.** 404 unknown vault.

### POST /api/v1/vaults/{vault_id}/truncate

Empty a vault without deleting it (notes, memory units, entities, assets). <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="244-257" />

- **Auth.** `require_delete`.
- **Path params.** `vault_id` (UUID).
- **Returns.** `{status: "success", deleted: {notes, memory_units, entities, ...}}`.
- **Errors.** 404 unknown vault.

### POST /api/v1/vaults/{identifier}/set-writer

Runtime override for the active write vault. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="260-271" />

- **Auth.** `require_write`.
- **Path params.** `identifier` (UUID or name).
- **Returns.** `{status: "success", active_vault: <UUID string>}`.
- **Lifetime.** Lost on server restart — `server.default_active_vault` re-applies.

### POST /api/v1/vaults/{identifier}/set-reader

Runtime override for the default read vault. <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="274-291" />

- **Auth.** `require_write`.
- **Path params.** `identifier` (UUID or name).
- **Returns.** `{status: "success", default_reader_vault: <UUID string>}`.
- **Lifetime.** Lost on server restart — `server.default_reader_vault` re-applies.

### GET /api/v1/vaults/{vault_id}/summary

Get the cached vault summary. <code-ref path="packages/core/src/memex_core/server/vault_summary.py" lines="36-56" />

- **Auth.** `require_read`.
- **Path params.** `vault_id` (UUID).
- **Returns.** `VaultSummaryDTO` — `id`, `vault_id`, `narrative`, `themes`, `inventory`, `key_entities`, `version`, `notes_incorporated`, `created_at`, `updated_at`.
- **Errors.** 404 no summary cached.

### POST /api/v1/vaults/{vault_id}/summary/regenerate

Rebuild the vault summary from every note. <code-ref path="packages/core/src/memex_core/server/vault_summary.py" lines="59-75" />

- **Auth.** `require_write`.
- **Path params.** `vault_id` (UUID).
- **Returns.** The fresh `VaultSummaryDTO`.

### GET /api/v1/vaults/{vault_id}/session-briefing

Generate a token-budgeted session briefing for LLM agents. Composes vault summary, top entities with mental-model trends, KV facts, and available vaults. <code-ref path="packages/core/src/memex_core/server/session_briefing.py" lines="21-48" />

- **Auth.** `require_read`.
- **Path params.** `vault_id` (UUID).
- **Query params.** `budget` (integer, must be 1000 or 2000; default 2000), `project_id` (optional, scopes KV namespace).
- **Returns.** `{briefing: string, vault_id, budget}`.
- **Errors.** 422 invalid budget value.

---

## Lineage

### GET /api/v1/lineage/{entity_type}/{id}

Unified lineage endpoint. <code-ref path="packages/core/src/memex_core/server/resources.py" lines="74-103" />

- **Auth.** `require_read`.
- **Path params.** `entity_type` (`note|entity|memory_unit|observation|mental_model` — `entity` is rewritten internally to `mental_model`), `id` (UUID).
- **Query params.** `direction` (`upstream|downstream`, default `upstream`), `depth` (1-10, default 3), `limit` (1-500, default 10).
- **Returns.** `LineageResponse` — a recursive tree of `{entity_type, entity, derived_from: [...]}`.
- **Errors.** 400 invalid `entity_type`.

---

## Reflection

### POST /api/v1/reflections

Queue a reflection on one entity. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="53-77" />

- **Auth.** `require_write`.
- **Body** (`ReflectionRequest`): `entity_id` (UUID), optional `vault_id` (defaults to the global vault), `limit_recent_memories`.
- **Returns.** `ReflectionResultDTO` — `{entity_id, new_observations: [], status: "queued"}`.

### POST /api/v1/reflections/batch

Queue reflections on many entities. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="195-230" />

- **Auth.** `require_write`.
- **Body.** `{requests: ReflectionRequest[]}`.
- **Returns.** NDJSON; each line is `ReflectionResultDTO` with `status="queued"`.

### GET /api/v1/reflections

Inspect the reflection queue. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="233-270" />

- **Auth.** `require_write` (the queue is operator-facing).
- **Query params.** `limit` (1-500, default 10), `status` (`queued` only — other statuses return an empty stream), `vault_id` (repeat).
- **Returns.** NDJSON; each line is `ReflectionQueueDTO` — `entity_id`, `vault_id`, `priority_score`.

### POST /api/v1/reflections/claim

Claim queue items for processing via `SELECT ... FOR UPDATE SKIP LOCKED`. NDJSON stream. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="273-304" />

- **Auth.** `require_write`.
- **Query params.** `limit` (1-500, default 10), `vault_id` (single, optional).
- **Returns.** NDJSON; each line is the `ReflectionQueueDTO` of a claimed item.

### GET /api/v1/admin/reflection/dlq

List dead-lettered reflection tasks. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="312-345" />

- **Auth.** `require_delete` (operator scope).
- **Query params.** `limit` (1-500, default 50), `offset` (≥0, default 0), `vault_id` (single, optional).
- **Returns.** `DeadLetterItemDTO[]` — `id`, `entity_id`, `vault_id`, `priority_score`, `retry_count`, `max_retries`, `last_error`, `status`.

### POST /api/v1/admin/reflection/dlq/{item_id}/retry

Reset a dead-lettered item back to pending. <code-ref path="packages/core/src/memex_core/server/reflection.py" lines="348-378" />

- **Auth.** `require_delete`.
- **Path params.** `item_id` (UUID).
- **Returns.** The updated `DeadLetterItemDTO`.
- **Errors.** 404 unknown item or not in `dead_letter` status.

---

## Consolidation

### POST /api/v1/consolidation/tick

Run one or more consolidation ticks immediately. Operator-facing — there is no MCP or Hermes parallel. <code-ref path="packages/core/src/memex_core/server/consolidation.py" lines="47-73" />

- **Auth.** `require_write`.
- **Body** (`ConsolidationTickRequest`): optional `vault_id` (UUID — when omitted, every vault ticks sequentially), `dry_run` (bool, default `false`), `budget` (1-10000, default config value).
- **Returns.** `{ticks: [...]}`. Per-vault failures appear as `{vault_id, error}` entries instead of breaking the batch.

### GET /api/v1/consolidation/status

Last-run timestamps per vault. <code-ref path="packages/core/src/memex_core/server/consolidation.py" lines="76-86" />

- **Auth.** `require_read`.
- **Query params.** `vault_id` (UUID, optional).
- **Returns.** `{ticks: [...]}`.

---

## Embeddings

### POST /api/v1/embed

Embed a text string with the active embedding model. <code-ref path="packages/core/src/memex_core/server/kv.py" lines="36-46" />

- **Auth.** `require_read`.
- **Body.** `{text: string}`.
- **Returns.** `{embedding: float[]}`. Length equals the configured embedding dimension.

---

## Resources

### GET /api/v1/resources/{path}

Stream a raw filestore resource (image, PDF, audio, …). <code-ref path="packages/core/src/memex_core/server/resources.py" lines="22-39" />

- **Auth.** `require_read`.
- **Path params.** `path` (filestore path; `path:path` matcher — slashes are allowed).
- **Returns.** Raw body with `Content-Type` guessed from the extension (falls back to `application/octet-stream`).
- **Errors.** 404 file not found or empty path.

---

## Diagnostics

The diagnostics router lives at `/api/v1/diagnostics` and requires `require_read` on every entry. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="33" />

### GET /api/v1/diagnostics/manifold/{vault_id}

UMAP projection — warm cache returns 200, cold cache returns 202 with a task ID. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="45-71" />

- **Auth.** `require_read` + vault access.
- **Path params.** `vault_id` (UUID).
- **Query params.** `force_refresh` (bool, default `false`).
- **Returns.**
  - 200 `{...projection payload...}` when warm.
  - 202 `{task_id, ...}` when compute kicked off.
- **Errors.** 501 `umap-learn not installed; install memex[diagnostics]`.

### GET /api/v1/diagnostics/manifold/{vault_id}/status

Poll an in-flight manifold compute. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="74-98" />

- **Auth.** `require_read` + vault access.
- **Path params.** `vault_id` (UUID).
- **Query params.** `task_id` (string, **required**).
- **Returns.** 200 when ready (payload); 202 when still computing; 404 when task and cache are both absent.
- **Errors.** 501 if `umap-learn` is not installed.

### GET /api/v1/diagnostics/retrieval/{vault_id}

Top-N entities by outcome volume. Independent of the UMAP cache. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="101-113" />

- **Auth.** `require_read` + vault access.
- **Path params.** `vault_id` (UUID).
- **Query params.** `top_n` (1-500, default 50).
- **Returns.** A heatmap dict.

### GET /api/v1/diagnostics/summary/{vault_id}

Synchronous diagnostics summary blob. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="116-127" />

- **Auth.** `require_read` + vault access.
- **Path params.** `vault_id` (UUID).
- **Returns.** A dict; no UMAP block.

### GET /api/v1/diagnostics/lint/{vault_id}

Lint-dashboard pivot. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="130-147" />

- **Auth.** `require_read` + vault access.
- **Path params.** `vault_id` (UUID).
- **Returns.** A dict with the `(lint_type, status, source)` pivot, the pending-by-type slice, and the top-5 most-recent pending findings.

---

## Stats

### GET /api/v1/stats/counts

System-wide counts. <code-ref path="packages/core/src/memex_core/server/stats.py" lines="17-27" />

- **Auth.** `require_read`.
- **Query params.** `vault_id` (repeat).
- **Returns.** `SystemStatsCountsDTO` — `notes`, `memory_units`, `entities`, `reflection_queue` (per-vault when filtered).

---

## Admin

### GET /api/v1/admin/audit

Query the audit log. <code-ref path="packages/core/src/memex_core/server/audit.py" lines="30-68" />

- **Auth.** `require_admin_auth` — admin-policy API key required, even when global auth is disabled. <code-ref path="packages/core/src/memex_core/server/audit.py" lines="16" />
- **Query params.** `actor`, `action`, `resource_type`, `since` (ISO 8601), `until` (ISO 8601), `limit` (1-500, default 50), `offset` (≥0, default 0).
- **Returns.** `AuditEntryDTO[]` — `id`, `timestamp`, `actor`, `action`, `resource_type`, `resource_id`, `session_id`, `details`.

### GET /api/v1/system/config

Resolved server config with secrets redacted. <code-ref path="packages/core/src/memex_core/server/system_routes.py" lines="28-43" />

- **Auth.** `require_admin_auth`.
- **Returns.** The result of `MemexConfig.model_dump(mode="json")`, run through `memex_common.redaction.redact`. Pydantic v2 already serializes `SecretStr` to `'**********'`; `redact` adds `<key>_set` siblings so callers can ask "is this configured?" without seeing the value.

---

## Health

### GET /api/v1/health

Liveness probe. <code-ref path="packages/core/src/memex_core/server/health.py" lines="19-22" />

- **Auth.** None — exempt from the auth middleware.
- **Returns.** `{status: "ok"}` (200).

### GET /api/v1/ready

Readiness probe. Checks the database, the filestore, and (when enabled) the tracing exporter. <code-ref path="packages/core/src/memex_core/server/health.py" lines="25-60" />

- **Auth.** None — exempt.
- **Returns.**
  - 200 `{status: "ok", database: "ok", filestore: "ok", tracing?: "ok"}` when every check passes.
  - 503 `{status: "unavailable", database: "ok|unavailable", filestore: "ok|unavailable", tracing?: "ok|unavailable"}` otherwise.

### GET /api/v1/metrics

Prometheus-compatible metrics. Registered by `prometheus-fastapi-instrumentator`, not by a router handler. <code-ref path="packages/core/src/memex_core/server/__init__.py" lines="293" />

- **Auth.** None — exempt.
- **Returns.** Prometheus text-format response.

---

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Configure the API key](../how-to/configuring-server/api-key.md)
- [Reference: Configuration](configuration.md)
- [Explanation: Hindsight framework](../explanation/hindsight-framework.md)
