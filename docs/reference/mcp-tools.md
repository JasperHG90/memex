# MCP Tools Reference

The Memex MCP server exposes 22 tools to AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io/). The server is implemented with [FastMCP](https://github.com/jlowin/fastmcp).

## Running the MCP Server

```bash
# stdio transport (default, for Claude Code / IDEs)
memex mcp run

# SSE transport
memex mcp run --transport sse --port 8080
```

## Recommended Workflow

1. **Discovery**: `memex_search` for global facts/entities; `memex_note_search` for source documents.
2. **Reading**: `memex_get_page_index` (table of contents) then `memex_get_node` (section text). Only fall back to `memex_read_note` for small notes.
3. **Avoid**: Do not use `memex_list_notes` for discovery.

---

## Search Tools

### `memex_search`

Search memory units (facts, events, observations) via multi-strategy TEMPR retrieval.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | The search query. |
| `limit` | int | No | `10` | Maximum number of results to return. |
| `vault_ids` | string[] | No | - | List of vault UUIDs or names to search in. |
| `token_budget` | int | No | - | Token budget for retrieval. |
| `strategies` | string[] | No | all | Strategies to run: `semantic`, `keyword`, `graph`, `temporal`, `mental_model`. |

Returns formatted text with Unit IDs, Note IDs, scores, and dates.

---

### `memex_note_search`

Search source notes by hybrid retrieval (semantic + keyword + graph + temporal). Returns ranked notes with snippets.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | The note search query. |
| `limit` | int | No | `5` | Maximum number of notes to return. |
| `expand_query` | bool | No | `false` | Enable multi-query expansion via LLM. |
| `reason` | bool | No | `false` | Identify relevant sections with reasoning. |
| `summarize` | bool | No | `false` | Synthesize an answer from retrieved sections (implies `reason=true`). |
| `vault_ids` | string[] | No | - | List of vault UUIDs or names to search in. |

Returns note titles, IDs, scores, snippets, relevant sections (when `reason=true`), and a synthesized answer (when `summarize=true`).

---

## Note Reading Tools

### `memex_get_page_index`

Get the hierarchical page index (table of contents) for a note. Returns section titles, summaries, token estimates, and node IDs. Use node IDs with `memex_get_node` to retrieve specific section text.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note. |

---

### `memex_get_node`

Retrieve the full text content of a specific note section (node) by its ID. Node IDs are found in search results (reasoning field) or via `memex_get_page_index`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `node_id` | string | Yes | The UUID of the node to retrieve. |

---

### `memex_read_note`

Retrieve the full content and metadata of a note by its UUID. This is a fallback — prefer `memex_get_page_index` + `memex_get_node` to read specific sections.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note to retrieve. |

---

## Note Management Tools

### `memex_add_note`

Add a note to the Memex knowledge base. Confirm the target vault with the user before calling; use `memex_active_vault` to check or `memex_list_vaults` to enumerate.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | string | Yes | - | The title of the note. |
| `markdown_content` | string | Yes | - | Note content in markdown. Use `memex_get_template` to get the expected structure. |
| `description` | string | Yes | - | Summary of note content (max 250 words). Cover context/intent and key insights. |
| `author` | string | Yes | - | Name of the model authoring this note. |
| `tags` | string[] | Yes | - | Tags for easier retrieval. |
| `supporting_files` | string[] | No | - | Absolute paths to supporting files (images, CSVs). |
| `vault_id` | string | No | Active vault | UUID of the vault to add the note to. |
| `note_key` | string | No | - | Unique stable key for incremental updates. |
| `background` | bool | No | `false` | Queue ingestion in background. |

---

### `memex_get_template`

Retrieve a markdown template for note creation. Use the returned template as the structure for `memex_add_note`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | Yes | Template type: `technical_brief`, `general_note`, `architectural_decision_record`, `request_for_comments`, `quick_note`. |

---

## Asset & Resource Tools

### `memex_list_assets`

List all file assets (images, PDFs, etc.) attached to a note.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note. |

Returns filenames, MIME types, and paths. Use paths with `memex_get_resource` to retrieve file content.

---

### `memex_get_resource`

Retrieve a file resource (image, audio, or document) by its path. Get asset paths from `memex_list_assets` (for notes) or `memex_get_lineage` (for memory units).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | The path to the resource file. |

Returns an `Image`, `Audio`, or `File` object depending on MIME type.

---

## Entity Tools

### `memex_list_entities`

List or search entities in the knowledge graph. Without a query, returns top entities by relevance.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | No | - | Search term to filter by name. |
| `limit` | int | No | `20` | Maximum entities to return. |
| `vault_id` | string | No | - | Vault UUID or name to filter by. |

---

### `memex_get_entity`

Get details for a specific entity by its UUID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The UUID of the entity. |

Returns entity name, ID, mention count, and vault.

---

### `memex_get_entity_mentions`

Get memory units that mention a specific entity.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `entity_id` | string | Yes | - | The UUID of the entity. |
| `limit` | int | No | `10` | Maximum mentions to return. |

---

### `memex_get_entity_cooccurrences`

Get entities that frequently co-occur with a given entity.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The UUID of the entity. |

---

## Memory Unit Tools

### `memex_get_memory_unit`

Retrieve a specific memory unit by its UUID. Returns the unit text, type, status, dates, metadata, and source note ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `unit_id` | string | Yes | The UUID of the memory unit. |

---

## Lineage & Provenance

### `memex_get_lineage`

Retrieve the provenance chain (lineage) of a memory unit, observation, note, or mental model. Shows the derivation tree from source notes through extraction to observations and mental models.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `unit_id` | string | Yes | - | The UUID of the memory unit or observation. |
| `entity_type` | string | No | `memory_unit` | Entity type: `memory_unit`, `observation`, `note`, `mental_model`. |

---

## Reflection

### `memex_reflect`

Trigger reflection on an entity to synthesize and update its mental model from recent memories. Reflection runs automatically in the background, but this tool triggers it immediately.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `entity_id` | string | Yes | - | The UUID of the entity to reflect upon. |
| `limit` | int | No | `20` | Limit recent memories to consider. |
| `vault_id` | string | No | Global Vault | The UUID of the vault. |

---

## Ingestion Tools

### `memex_ingest_url`

Ingest content from a URL into Memex.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | The URL to ingest. |
| `vault_id` | string | No | Active vault | Target vault UUID. |
| `background` | bool | No | `true` | Queue ingestion in background. |

---

### `memex_batch_ingest`

Asynchronously ingest multiple local files into Memex.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `file_paths` | string[] | Yes | - | List of absolute paths to local files. |
| `vault_id` | string | No | - | UUID of the vault to ingest into. |
| `batch_size` | int | No | `32` | Number of files to process per chunk. |

---

### `memex_get_batch_status`

Retrieve the status and results of a batch ingestion job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `job_id` | string | Yes | The UUID of the batch job. |

Returns job status, progress, processed/skipped/failed counts, created note IDs, and errors.

---

## Vault Tools

### `memex_active_vault`

Retrieve the currently active vault information. No parameters.

Returns the active vault name and ID.

---

### `memex_list_vaults`

List all available vaults. No parameters.

Returns vault names, IDs, and descriptions.

---

### `memex_list_notes`

List notes in the active vault. Not recommended for discovery -- use `memex_search` or `memex_note_search` instead.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | int | No | `20` | Maximum notes to return. |
| `offset` | int | No | `0` | Pagination offset. |
| `vault_id` | string | No | - | Vault UUID or name to filter by. |
