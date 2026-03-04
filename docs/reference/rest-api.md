# REST API Reference

The Memex Core API is built with [FastAPI](https://fastapi.tiangolo.com/). All endpoints are prefixed with `/api/v1`.

## Interactive Documentation

When the server is running:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## Response Format

Most collection endpoints use **newline-delimited JSON (NDJSON)** streaming (`application/x-ndjson`). Each line in the response is a separate JSON object. Single-resource endpoints return standard JSON (`application/json`).

## Authentication

When `server.auth.enabled` is `true` in the configuration, all requests (except exempt paths) require the `X-API-Key` header.

```bash
curl -H "X-API-Key: your-key-here" http://localhost:8000/api/v1/health
```

Exempt paths (no authentication required):
- `/api/v1/health`
- `/api/v1/ready`
- `/api/v1/metrics`
- `/docs`, `/redoc`, `/openapi.json`

### Error Responses

| Status | Description |
|--------|-------------|
| `401` | Missing `X-API-Key` header. |
| `403` | Invalid API key. |

## Session Tracking

All requests may include an `X-Session-ID` header. If omitted, the server generates one. The session ID is returned in the response headers and used for log correlation.

## Rate Limiting

When `server.rate_limit.enabled` is `true`, requests are rate-limited by client IP. The `X-RateLimit-*` headers in responses indicate current limits. Health, readiness, and metrics endpoints are exempt.

---

## Ingestion

### `POST /api/v1/ingestions`

Ingest a note artifact. Content and files must be Base64-encoded.

#### Request Body

```json
{
  "name": "My Note",
  "description": "A brief description",
  "content": "<base64-encoded-content>",
  "files": {
    "diagram.png": "<base64-encoded-bytes>"
  },
  "tags": ["research", "architecture"],
  "note_key": "unique-stable-key",
  "vault_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Name of the note. |
| `description` | string | Yes | Brief description. |
| `content` | string | Yes | Base64-encoded note content (UTF-8 markdown). |
| `files` | object | No | Map of filename to Base64-encoded file bytes. |
| `tags` | string[] | No | Tags for categorization. |
| `note_key` | string | No | Unique stable key for idempotent updates. |
| `vault_id` | UUID | No | Target vault. Uses active vault if omitted. |

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `background` | bool | `false` | When `true`, returns `202 Accepted` with a `job_id` for async processing. |

#### Response (200)

```json
{
  "note_id": "550e8400-e29b-41d4-a716-446655440000",
  "unit_ids": ["id1", "id2"],
  "status": "success",
  "reason": null
}
```

#### Response (202 — background mode)

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending"
}
```

#### Example

```bash
# Ingest a note (content must be base64-encoded)
curl -X POST http://localhost:8000/api/v1/ingestions \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Meeting Notes",
    "description": "Team sync 2025-01-15",
    "content": "'$(echo -n "# Meeting Notes\nDiscussed architecture." | base64)'",
    "tags": ["meeting"]
  }'
```

---

### `POST /api/v1/ingestions/url`

Ingest content from a URL (web scraping).

#### Request Body

```json
{
  "url": "https://example.com/article",
  "vault_id": "550e8400-e29b-41d4-a716-446655440000",
  "reflect_after": false,
  "assets": {
    "image.png": "<base64-encoded-bytes>"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | URL to scrape and ingest. |
| `vault_id` | UUID | No | Target vault. |
| `reflect_after` | bool | No | Trigger reflection after ingestion. |
| `assets` | object | No | Additional assets to attach (Base64-encoded). |

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `background` | bool | `false` | When `true`, returns `202 Accepted` immediately. |

#### Example

```bash
curl -X POST http://localhost:8000/api/v1/ingestions/url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'
```

---

### `POST /api/v1/ingestions/file`

Ingest content from a server-side file path.

#### Request Body

```json
{
  "file_path": "/path/to/document.pdf",
  "vault_id": "550e8400-e29b-41d4-a716-446655440000",
  "reflect_after": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | Yes | Absolute path to a file on the server. |
| `vault_id` | UUID | No | Target vault. |
| `reflect_after` | bool | No | Trigger reflection after ingestion. |

#### Response (200)

Returns `IngestResponse`.

---

### `POST /api/v1/ingestions/upload`

Upload and ingest files via multipart form data.

For a single non-markdown file, conversion is performed via MarkItDown. For multiple files, the main markdown file is identified by priority: `NOTE.md` > `README.md` > `index.md` > first `.md` file.

#### Request

- **Content-Type**: `multipart/form-data`
- **files**: One or more uploaded files.
- **metadata** (optional): JSON string with `name`, `description`, `tags`, `vault_id`.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `background` | bool | `false` | When `true`, returns `202 Accepted` immediately. |

#### Example

```bash
curl -X POST http://localhost:8000/api/v1/ingestions/upload \
  -F "files=@report.md" \
  -F "files=@chart.png" \
  -F 'metadata={"name": "Q4 Report", "tags": ["reports"]}'
```

---

### `POST /api/v1/ingestions/webhook`

Ingest a note from an external webhook. Accepts plain JSON (no Base64 encoding required).

#### Request Body

```json
{
  "title": "Alert: CPU spike",
  "description": "Production server alert",
  "content": "CPU usage exceeded 90% on prod-01.",
  "source": "monitoring",
  "tags": ["alert", "infrastructure"],
  "vault_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | Yes | Note title. |
| `content` | string | Yes | Plain text content (not Base64). |
| `source` | string | Yes | Source identifier (used for idempotent key generation). |
| `description` | string | No | Brief description. |
| `tags` | string[] | No | Tags for categorization. |
| `vault_id` | UUID | No | Target vault. |

#### Headers

| Header | Required | Description |
|--------|----------|-------------|
| `X-Webhook-Signature` | Conditional | `hex(HMAC-SHA256(secret, raw_body))`. Required when a webhook secret is configured in `server.auth.webhook_secret`. |

#### Response (202)

Returns `IngestResponse`.

#### Errors

| Status | Description |
|--------|-------------|
| `401` | Missing `X-Webhook-Signature` when a webhook secret is configured. |
| `403` | Invalid webhook signature. |
| `400` | Invalid webhook payload. |

---

### `POST /api/v1/ingestions/batch`

Start an asynchronous batch ingestion job.

#### Request Body

```json
{
  "notes": [
    {
      "name": "Note 1",
      "description": "First note",
      "content": "<base64-encoded>",
      "tags": ["batch"]
    }
  ],
  "vault_id": "550e8400-e29b-41d4-a716-446655440000",
  "batch_size": 32
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `notes` | NoteCreateDTO[] | Yes | Array of notes to ingest. |
| `vault_id` | UUID | No | Target vault. |
| `batch_size` | int | No | Number of notes to process per chunk. |

#### Response (202)

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending"
}
```

---

### `GET /api/v1/ingestions/{job_id}`

Retrieve the status of a batch ingestion job.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `job_id` | UUID | The batch job ID returned by the batch endpoint. |

#### Response (200)

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "progress": 1.0,
  "result": {
    "processed_count": 10,
    "skipped_count": 0,
    "failed_count": 0,
    "note_ids": ["id1", "id2"],
    "errors": []
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | One of: `pending`, `processing`, `completed`, `failed`. |
| `progress` | float | Completion fraction (0.0 to 1.0). |
| `result` | object | Present when status is `completed`. |

---

## Memories & Retrieval

### `POST /api/v1/memories/search`

Search the knowledge base using TEMPR retrieval strategies. Returns results as NDJSON stream.

#### Request Body

```json
{
  "query": "How does connection pooling work?",
  "limit": 10,
  "vault_ids": ["550e8400-e29b-41d4-a716-446655440000"],
  "token_budget": 4000,
  "strategies": ["semantic", "keyword", "graph"],
  "include_stale": false,
  "debug": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query. |
| `limit` | int | No | `5` | Maximum results. |
| `vault_ids` | UUID[] | No | - | Filter by vault IDs. |
| `token_budget` | int | No | - | Maximum tokens for retrieval context. |
| `strategies` | string[] | No | all | Strategies to use: `semantic`, `keyword`, `graph`, `temporal`, `mental_model`. |
| `include_stale` | bool | No | `false` | Include stale/superseded units. |
| `debug` | bool | No | `false` | Include per-strategy debug info in results. |

#### Response (200 — NDJSON)

Each line is a `MemoryUnitDTO`:

```json
{"id": "uuid", "text": "...", "fact_type": "fact", "score": 0.85, "note_id": "uuid", "source_note_ids": ["uuid"], "vault_id": "uuid", "metadata": {}, "mentioned_at": "2025-01-15T10:00:00Z"}
```

#### Example

```bash
curl -X POST http://localhost:8000/api/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query": "PostgreSQL connection pooling", "limit": 5}'
```

---

### `POST /api/v1/memories/summary`

Generate an AI summary with citations from search result texts.

#### Request Body

```json
{
  "query": "How does reflection work?",
  "texts": ["Reflection synthesizes observations...", "Mental models are updated..."]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | The original search query for context. |
| `texts` | string[] | Yes | Text passages to synthesize (typically from search results). |

#### Response (200)

```json
{
  "summary": "Reflection works by synthesizing observations about entities into mental models..."
}
```

---

### `GET /api/v1/memories/{id}`

Get details of a specific memory unit.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `id` | UUID | Memory unit ID. |

#### Response (200)

Returns a `MemoryUnitDTO`.

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Memory unit not found. |

---

### `DELETE /api/v1/memories/{id}`

Delete a memory unit and all associated data (entity links, memory links, evidence).

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `id` | UUID | Memory unit ID. |

#### Response (200)

```json
{"status": "success"}
```

---

## Notes

### `GET /api/v1/notes`

List notes. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `100` | Maximum notes to return. |
| `offset` | int | `0` | Pagination offset. |
| `sort` | string | - | Sort option. Use `-created_at` for most recent first. |
| `vault_id` | UUID (list) | - | Filter by vault ID(s). Repeat for multiple. |

#### Response (200 — NDJSON)

Each line is a `NoteDTO`:

```json
{"id": "uuid", "name": "Meeting Notes", "title": "Meeting Notes", "original_text": "...", "created_at": "2025-01-15T10:00:00Z", "vault_id": "uuid", "doc_metadata": {}}
```

#### Example

```bash
# List recent notes
curl "http://localhost:8000/api/v1/notes?sort=-created_at&limit=10"
```

---

### `POST /api/v1/notes/search`

Search notes using multi-channel fusion (RRF). Returns an NDJSON stream.

#### Request Body

```json
{
  "query": "database migration patterns",
  "limit": 5,
  "vault_ids": ["uuid"],
  "expand_query": false,
  "fusion_strategy": "rrf",
  "strategies": ["semantic", "keyword"],
  "strategy_weights": null,
  "reason": false,
  "summarize": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query. |
| `limit` | int | No | `5` | Maximum notes to return. |
| `vault_ids` | UUID[] | No | - | Filter by vault IDs. |
| `expand_query` | bool | No | `false` | Enable LLM-powered query expansion. |
| `fusion_strategy` | string | No | `rrf` | Fusion strategy: `rrf` or `position_aware`. |
| `strategies` | string[] | No | all | Strategies: `semantic`, `keyword`, `graph`, `temporal`. |
| `strategy_weights` | object | No | - | Custom weights per strategy. |
| `reason` | bool | No | `false` | Identify relevant sections with reasoning. |
| `summarize` | bool | No | `false` | Synthesize an answer (implies `reason=true`). |

#### Response (200 — NDJSON)

Each line is a `NoteSearchResult`:

```json
{"note_id": "uuid", "score": 0.92, "snippets": [{"text": "..."}], "metadata": {"name": "..."}, "reasoning": [{"node_uuid": "...", "reasoning": "..."}], "answer": null}
```

---

### `GET /api/v1/notes/{note_id}`

Get a note by ID.

#### Response (200)

Returns a `NoteDTO`.

---

### `GET /api/v1/notes/{note_id}/page-index`

Get the page index (hierarchical table of contents) for a note.

#### Response (200)

```json
{
  "note_id": "uuid",
  "page_index": [
    {
      "node_id": "uuid",
      "title": "Introduction",
      "level": 1,
      "token_estimate": 250,
      "summary": {"what": "Overview of the topic"},
      "children": []
    }
  ]
}
```

Use the `node_id` values with `GET /api/v1/nodes/{node_id}` to retrieve specific section text.

---

### `GET /api/v1/notes/{id}/lineage`

Get the provenance lineage of a note.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `direction` | string | `upstream` | `upstream` or `downstream`. |
| `depth` | int | `3` | Maximum recursion depth. |
| `limit` | int | `10` | Maximum children per node. |

#### Response (200)

Returns a `LineageResponse` tree.

---

### `GET /api/v1/nodes/{node_id}`

Get a specific page-index node (section) by its ID.

#### Response (200)

Returns a `NodeDTO`:

```json
{
  "id": "uuid",
  "note_id": "uuid",
  "title": "Section Title",
  "level": 2,
  "seq": 3,
  "status": "active",
  "text": "Full section text..."
}
```

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Node not found. |

---

### `DELETE /api/v1/notes/{note_id}`

Delete a note and all associated data (memory units, chunks, links, assets).

#### Response (200)

```json
{"status": "success"}
```

---

## Entities

### `GET /api/v1/entities`

List, search, or rank entities. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `100` | Maximum entities to return. |
| `q` | string | - | Search query for name-based filtering. |
| `sort` | string | - | Sort option. Use `-mentions` for top entities by mention count. |
| `vault_id` | UUID (list) | - | Filter by vault ID(s). Repeat for multiple. |

#### Examples

```bash
# List entities ranked by relevance
curl "http://localhost:8000/api/v1/entities?limit=20"

# Search by name
curl "http://localhost:8000/api/v1/entities?q=PostgreSQL"

# Top entities by mention count
curl "http://localhost:8000/api/v1/entities?sort=-mentions&limit=10"
```

---

### `GET /api/v1/entities/{id}`

Get details of a specific entity.

#### Response (200)

Returns an `EntityDTO`:

```json
{
  "id": "uuid",
  "name": "PostgreSQL",
  "mention_count": 42,
  "entity_type": "technology"
}
```

---

### `GET /api/v1/entities/{id}/mentions`

Get memory units and source notes that mention this entity. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `20` | Maximum mentions to return. |
| `vault_id` | UUID (list) | - | Filter by vault ID(s). |

#### Response (200 — NDJSON)

Each line contains a `unit` (MemoryUnitDTO) and `document` (NoteDTO).

---

### `GET /api/v1/entities/{id}/cooccurrences`

Get co-occurrence edges for a specific entity. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `vault_id` | UUID (list) | - | Filter by vault ID(s). |

#### Response (200 — NDJSON)

```json
{"entity_id_1": "uuid", "entity_id_2": "uuid", "cooccurrence_count": 5, "vault_id": "uuid"}
```

---

### `GET /api/v1/cooccurrences`

Get co-occurrences for multiple entities in bulk. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ids` | string | Yes | Comma-separated entity UUIDs. |
| `vault_id` | UUID (list) | No | Filter by vault ID(s). |

#### Example

```bash
curl "http://localhost:8000/api/v1/cooccurrences?ids=uuid1,uuid2,uuid3"
```

---

### `GET /api/v1/entities/{id}/lineage`

Get the lineage of an entity (resolves as `mental_model` type internally).

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `direction` | string | `upstream` | `upstream` or `downstream`. |
| `depth` | int | `3` | Maximum recursion depth. |
| `limit` | int | `10` | Maximum children per node. |

#### Response (200)

Returns a `LineageResponse`.

---

### `DELETE /api/v1/entities/{entity_id}`

Delete an entity and all associated data (mental models, aliases, links, co-occurrences).

#### Response (200)

```json
{"status": "success"}
```

---

### `DELETE /api/v1/entities/{entity_id}/mental-model`

Delete the mental model for a specific entity in a vault.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `vault_id` | UUID | Active vault | Vault to target. |

#### Response (200)

```json
{"status": "success"}
```

---

## Reflections

### `POST /api/v1/reflections`

Trigger reflection on a single entity. Reflection runs as a background task.

#### Request Body

```json
{
  "entity_id": "550e8400-e29b-41d4-a716-446655440000",
  "vault_id": "550e8400-e29b-41d4-a716-446655440000",
  "limit_recent_memories": 20
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | UUID | Yes | Entity to reflect on. |
| `vault_id` | UUID | No | Vault context. Defaults to global vault. |
| `limit_recent_memories` | int | No | Number of recent memories to consider. |

#### Response (200)

```json
{
  "entity_id": "uuid",
  "new_observations": [],
  "status": "queued"
}
```

---

### `POST /api/v1/reflections/batch`

Trigger reflection on multiple entities. Returns an NDJSON stream.

#### Request Body

```json
{
  "requests": [
    {"entity_id": "uuid1", "vault_id": "uuid"},
    {"entity_id": "uuid2"}
  ]
}
```

#### Response (200 — NDJSON)

Each line is a `ReflectionResultDTO` with `status: "queued"`.

---

### `GET /api/v1/reflections`

List items from the reflection queue. Returns an NDJSON stream.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `10` | Maximum items to return. |
| `status` | string | - | Filter by status. Use `queued` for pending items. |
| `vault_id` | UUID (list) | - | Filter by vault ID(s). |

#### Response (200 — NDJSON)

Each line is a `ReflectionQueueDTO`:

```json
{"entity_id": "uuid", "vault_id": "uuid", "priority_score": 0.85}
```

---

### `POST /api/v1/reflections/claim`

Claim reflection queue items for processing. Uses `SELECT ... FOR UPDATE SKIP LOCKED` for atomic claiming.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `10` | Maximum items to claim. |

#### Response (200 — NDJSON)

Each line is a claimed `ReflectionQueueDTO`.

---

## Vaults

### `GET /api/v1/vaults`

List vaults. Returns an NDJSON stream. Supports filtering by state.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `state` | string | - | Use `active` to return only the active (writer) vault. |
| `is_default` | bool | - | Use `true` to return the active vault plus all attached (read) vaults. |

#### Examples

```bash
# List all vaults
curl http://localhost:8000/api/v1/vaults

# Get only the active vault
curl "http://localhost:8000/api/v1/vaults?state=active"

# Get default vaults (active + attached)
curl "http://localhost:8000/api/v1/vaults?is_default=true"
```

---

### `POST /api/v1/vaults`

Create a new vault.

#### Request Body

```json
{
  "name": "my-project",
  "description": "Notes for my-project development"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Vault name. |
| `description` | string | No | Vault description. |

#### Response (200)

Returns a `VaultDTO`:

```json
{
  "id": "uuid",
  "name": "my-project",
  "description": "Notes for my-project development"
}
```

---

### `GET /api/v1/vaults/{identifier}`

Get a vault by UUID or resolve a vault name to its UUID.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `identifier` | string | UUID or vault name. |

#### Response (200)

```json
{"id": "550e8400-e29b-41d4-a716-446655440000"}
```

---

### `DELETE /api/v1/vaults/{vault_id}`

Delete a vault.

#### Response (200)

```json
{"status": "success"}
```

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Vault not found. |

---

### `POST /api/v1/vaults/{identifier}/set-writer`

Set the active (writer) vault for the current server session. This is a runtime override; on restart, config file values apply again.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `identifier` | string | Vault name or UUID. |

#### Response (200)

```json
{"status": "success", "active_vault": "uuid"}
```

---

### `POST /api/v1/vaults/{identifier}/toggle-attached`

Attach or detach a vault for read-only search inclusion. This is a runtime override.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `identifier` | string | Vault name or UUID. |

#### Query Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `attach` | bool | Yes | `true` to attach, `false` to detach. |

#### Response (200)

```json
{"status": "success", "attached_vaults": ["vault-a", "vault-b"]}
```

---

## Lineage

### `GET /api/v1/lineage/{entity_type}/{id}`

Get the provenance lineage of any entity type.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `entity_type` | string | One of: `note`, `entity`, `memory_unit`, `observation`, `mental_model`. |
| `id` | UUID | Entity ID. |

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `direction` | string | `upstream` | `upstream` or `downstream`. |
| `depth` | int | `3` | Maximum recursion depth. |
| `limit` | int | `10` | Maximum children per node. |

#### Response (200)

Returns a `LineageResponse` — a recursive tree structure:

```json
{
  "entity_type": "memory_unit",
  "entity": {"id": "uuid", "text": "..."},
  "derived_from": [
    {
      "entity_type": "note",
      "entity": {"id": "uuid", "name": "..."},
      "derived_from": []
    }
  ]
}
```

#### Errors

| Status | Description |
|--------|-------------|
| `400` | Invalid entity type. |

---

## Resources

### `GET /api/v1/resources/{path}`

Retrieve a raw resource file (image, PDF, etc.) from the filestore.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `path` | string | File path within the filestore. |

#### Response (200)

Returns the raw file content with an appropriate MIME type (e.g., `image/png`, `application/pdf`).

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Resource not found. |

---

## Statistics

### `GET /api/v1/stats/counts`

Get system-wide counts for notes, memory units, entities, and reflection queue.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `vault_id` | UUID (list) | - | Filter counts by vault ID(s). |

#### Response (200)

```json
{
  "memories": 1234,
  "entities": 567,
  "reflection_queue": 12
}
```

---

### `GET /api/v1/stats/token-usage`

Get daily aggregated LLM token usage.

#### Response (200)

```json
{
  "usage": [
    {"date": "2025-01-15", "total_tokens": 45000},
    {"date": "2025-01-14", "total_tokens": 32000}
  ]
}
```

---

## Health & Monitoring

### `GET /api/v1/health`

Liveness probe. Returns `200` if the process is running.

#### Response (200)

```json
{"status": "ok"}
```

---

### `GET /api/v1/ready`

Readiness probe. Returns `200` when the database is reachable, `503` otherwise.

#### Response (200)

```json
{"status": "ok"}
```

#### Response (503)

```json
{"status": "unavailable"}
```

---

### `GET /api/v1/metrics`

Prometheus-compatible metrics endpoint. Exposed by `prometheus-fastapi-instrumentator`.

---

## Webhooks (CRUD)

Manage outgoing webhook subscriptions. Memex delivers event notifications to registered URLs.

### `POST /api/v1/webhooks`

Register a new webhook endpoint.

#### Request Body

```json
{
  "url": "https://example.com/webhook",
  "secret": "your-secret-key-min-16-chars",
  "events": ["note.created", "reflection.completed"],
  "active": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string (HTTPS) | Yes | Delivery URL. |
| `secret` | string | Yes | Shared secret for HMAC signing (16-255 characters). |
| `events` | string[] | Yes | Event types to subscribe to (minimum 1). |
| `active` | bool | No | Whether the webhook starts enabled (default: `true`). |

#### Response (201)

Returns a `WebhookDTO`.

---

### `GET /api/v1/webhooks`

List all registered webhooks.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `active_only` | bool | `false` | Only return active webhooks. |

#### Response (200)

Returns `WebhookDTO[]`.

---

### `GET /api/v1/webhooks/{webhook_id}`

Get a webhook by ID.

#### Response (200)

Returns a `WebhookDTO`:

```json
{
  "id": "uuid",
  "url": "https://example.com/webhook",
  "events": ["note.created"],
  "active": true,
  "created_at": "2025-01-15T10:00:00Z"
}
```

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Webhook not found. |

---

### `PATCH /api/v1/webhooks/{webhook_id}`

Update an existing webhook. All fields are optional.

#### Request Body

```json
{
  "url": "https://example.com/new-webhook",
  "events": ["note.created", "note.deleted"],
  "active": false
}
```

#### Response (200)

Returns the updated `WebhookDTO`.

---

### `DELETE /api/v1/webhooks/{webhook_id}`

Delete a webhook registration.

#### Response (204)

No content.

---

### `GET /api/v1/webhooks/{webhook_id}/deliveries`

List delivery records for a webhook (history of event deliveries and their status).

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `50` | Maximum deliveries to return (1-500). |
| `offset` | int | `0` | Pagination offset. |

#### Response (200)

Returns `WebhookDeliveryDTO[]`:

```json
[
  {
    "id": "uuid",
    "webhook_id": "uuid",
    "event": "note.created",
    "payload": {},
    "status": "delivered",
    "attempts": 1,
    "last_error": null,
    "created_at": "2025-01-15T10:00:00Z"
  }
]
```

---

## Admin

### `GET /api/v1/admin/audit`

Query the audit log with optional filters.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `actor` | string | - | Filter by actor (e.g., API key prefix). |
| `action` | string | - | Filter by action (e.g., `auth.success`, `note.create`). |
| `resource_type` | string | - | Filter by resource type. |
| `since` | datetime | - | Only entries after this time (ISO 8601). |
| `until` | datetime | - | Only entries before this time (ISO 8601). |
| `limit` | int | `50` | Maximum entries to return (1-500). |
| `offset` | int | `0` | Pagination offset. |

#### Response (200)

Returns `AuditEntryDTO[]`:

```json
[
  {
    "id": "uuid",
    "timestamp": "2025-01-15T10:00:00Z",
    "actor": "abc12345...",
    "action": "auth.success",
    "resource_type": "note",
    "resource_id": "uuid",
    "session_id": "session-uuid",
    "details": {"path": "/api/v1/notes", "method": "GET"}
  }
]
```

#### Example

```bash
# Get recent audit entries for authentication events
curl "http://localhost:8000/api/v1/admin/audit?action=auth.failure&since=2025-01-15T00:00:00Z&limit=20"
```

---

### `GET /api/v1/admin/reflection/dlq`

List dead-lettered reflection tasks that exhausted their retries.

#### Query Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | `50` | Maximum entries (1-500). |
| `offset` | int | `0` | Pagination offset. |
| `vault_id` | UUID | - | Filter by vault ID. |

#### Response (200)

Returns `DeadLetterItemDTO[]`:

```json
[
  {
    "id": "uuid",
    "entity_id": "uuid",
    "vault_id": "uuid",
    "priority_score": 0.85,
    "retry_count": 3,
    "max_retries": 3,
    "last_error": "Connection timeout",
    "status": "dead_letter"
  }
]
```

---

### `POST /api/v1/admin/reflection/dlq/{item_id}/retry`

Reset a dead-lettered reflection item back to pending for re-processing.

#### Path Parameters

| Name | Type | Description |
|------|------|-------------|
| `item_id` | UUID | Dead letter item ID. |

#### Response (200)

Returns the updated `DeadLetterItemDTO` with status reset.

#### Errors

| Status | Description |
|--------|-------------|
| `404` | Item not found or not in dead_letter status. |
