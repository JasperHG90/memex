# MCP Tools Reference

The Memex MCP server exposes 35 tools to AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io/). The server is implemented with [FastMCP](https://github.com/jlowin/fastmcp).

## Progressive Disclosure (Opt-In)

When enabled, `tools/list` returns 3 discovery meta-tools instead of all 35 tool schemas:

- **`memex_tags`** — browse 7 tool categories (`search`, `read`, `write`, `browse`, `assets`, `entities`, `storage`)
- **`memex_search(query, tags=[...])`** — find tools by keyword (BM25), optionally filtered by tag
- **`memex_get_schema(tools=[...])`** — get parameter details for specific tools

Real tools remain directly callable by name via `tools/call`. Set `MEMEX_MCP_PROGRESSIVE_DISCLOSURE=true` to enable progressive disclosure (meta-tools on `tools/list`, real tools hidden but still callable).

## Running the MCP Server

```bash
# stdio transport (default, for Claude Code / IDEs)
memex mcp run

# SSE transport
memex mcp run --transport sse --port 8080
```

## Recommended Workflow

Follow this three-step retrieval workflow:

1. **Search** — Pick by query type, or run both in parallel when unsure:
   - `memex_memory_search` (memory search) for broad/exploratory queries
   - `memex_note_search` (note search) for targeted document retrieval
2. **Filter** — Call `memex_get_notes_metadata` on candidate notes (cheap, ~50 tokens each). Check title, tags, description to confirm relevance before reading. Skip after `memex_note_search` — metadata is already inline.
3. **Read** — Only for confirmed-relevant notes: `memex_get_page_indices` (TOC + node IDs) then `memex_get_nodes` (section text). Fall back to `memex_read_note` only for small notes (< 500 tokens).
4. **Avoid**: Do not use `memex_recent_notes` for discovery.

### When to use which search

| Tool | Best for | Returns |
|------|----------|---------|
| `memex_memory_search` | Broad exploration ("What do I know about X?"), factual recall ("When did Y happen?") | Individual facts, events, observations across all notes |
| `memex_note_search` | Targeted document retrieval ("Which note describes X?"), deep-diving into a topic | Whole source notes ranked by relevance with snippets |

When unsure which to use, run both in parallel and combine results (deduplicate by Note ID).

### When to use which reading tool

| Tool | Cost | Best for | Returns |
|------|------|----------|---------|
| `memex_get_notes_metadata` | ~50 tokens/note | Relevance filtering — checking tags, title, dates | Metadata for 1+ notes |
| `memex_get_page_indices` + `memex_get_nodes` | ~500+ tokens | Section-level reading of note content | TOC tree, then section text |
| `memex_read_note` | Full note | Reading a small note in full (fallback) | Full note content |

Always call `memex_get_notes_metadata` before `memex_get_page_indices` to avoid wasting tokens on irrelevant notes.

---

## Search Tools

### `memex_memory_search`

Search memory units (facts, events, observations) via multi-strategy TEMPR retrieval. Best for broad exploration across all notes and precise factual recall.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | The search query. |
| `limit` | int | No | `10` | Maximum number of results to return. |
| `vault_ids` | string[] | No | from config | List of vault UUIDs or names to search in. Defaults to `config.read_vaults`. |
| `token_budget` | int | No | - | Token budget for retrieval. |
| `strategies` | string[] | No | all | Strategies to run: `semantic`, `keyword`, `graph`, `temporal`, `mental_model`. |
| `include_superseded` | bool | No | `false` | Include superseded (low-confidence) memory units. |
| `after` | string | No | - | Only results after this ISO 8601 date (e.g. `2025-01-01`). |
| `before` | string | No | - | Only results before this ISO 8601 date (e.g. `2025-12-31`). |
| `tags` | string[] | No | - | Only results from notes with ALL of these tags. |
| `include_seen` | bool | No | `true` | Include previously returned results in full. Set to `false` to compress already-seen results. |
| `source_context` | string | No | - | Filter by source context (e.g. `"user_notes"` to search only user annotations). |

Returns formatted text with Unit IDs, Note IDs (with titles), scores, and dates. Each memory unit includes `links` — typed relationships (contradicts, reinforces, temporal, causes, etc.) to other units, with weight and source note metadata.

All vault parameters are optional and default to the resolved config values (`config.write_vault` for writes, `config.read_vaults` for reads).

---

### `memex_note_search`

Search source notes by hybrid retrieval (semantic + keyword + graph + temporal). Returns ranked notes with snippets. Best for targeted document retrieval and deep-diving into a topic.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | The note search query. |
| `limit` | int | No | `10` | Maximum number of notes to return. |
| `expand_query` | bool | No | `false` | Enable multi-query expansion via LLM. |
| `vault_ids` | string[] | No | from config | List of vault UUIDs or names to search in. Defaults to `config.read_vaults`. |
| `strategies` | string[] | No | all | Strategies: `semantic`, `keyword`, `graph`, `temporal`. |
| `after` | string | No | - | Only notes after this ISO 8601 date. |
| `before` | string | No | - | Only notes before this ISO 8601 date. |
| `tags` | string[] | No | - | Only notes with ALL of these tags. |
| `include_seen` | bool | No | `true` | Include previously returned results in full. Set to `false` to compress already-seen results. |

Returns note titles, IDs, scores, snippets, and inline metadata. Results include `related_notes` (notes sharing entities, ranked by specificity) and `links` (typed relationships from memory_links table). Use these to follow relationship chains without additional queries.

---

### `memex_search_user_notes`

Search only your own annotations (user_notes) across all notes. Returns memory units extracted from user_notes. Use this to recall what you yourself have been thinking or annotating.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query. |
| `vault_ids` | string[] | No | from config | Vault UUIDs or names. Use `"*"` for all vaults. Omit to use config defaults. |
| `limit` | int | No | `10` | Maximum results to return. |

Returns the same format as `memex_memory_search`, filtered to `source_context='user_notes'`.

---

### `memex_find_note`

Lightweight fuzzy title search. Returns matching note titles, IDs, and scores. Use when you know (part of) the title. For content search, use `memex_note_search`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Title search query (partial or fuzzy match). |
| `vault_ids` | string[] | No | - | Vault UUIDs or names to search in. `null` = all vaults. |
| `limit` | int | No | `5` | Maximum results to return. |

Returns note titles, IDs, similarity scores, status, and publish dates.

---

### `memex_survey`

Survey a broad topic. Decomposes into 3-5 focused sub-questions, runs parallel searches, deduplicates, and returns facts grouped by source note. Use for panoramic queries like "what do you know about X?" instead of making many manual search calls.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Broad topic or panoramic query to survey. |
| `vault_ids` | string[] | No | from config | Vault UUIDs or names. Use `"*"` for all vaults. Omit to use config defaults. |
| `limit_per_query` | int | No | `10` | Max results per sub-question. |
| `token_budget` | int | No | - | Max token budget for all results. Truncates when exceeded. |

Returns facts grouped by source note with sub-question decomposition.

---

## Note Reading Tools

### `memex_get_notes_metadata`

Get metadata (title, tags, token count, has_assets) for 1+ notes. Use after `memex_memory_search` to filter results before reading. Skip after `memex_note_search` (metadata already inline).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_ids` | string[] | Yes | List of Note UUIDs. |

Returns metadata for each note, or errors for invalid/missing IDs.

---

### `memex_get_page_indices`

Get the hierarchical page index (table of contents) for 1+ notes. Returns metadata plus section titles, summaries, token estimates, and node IDs. Includes `related_notes` — other notes sharing entities with this one, ranked by specificity. Use node IDs with `memex_get_nodes` to retrieve specific section text. Only call after `memex_get_notes_metadata` confirms relevance.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `note_ids` | string[] | Yes | - | List of Note UUIDs. |
| `depth` | int | No | - | Max tree depth to return (0=top-level H1+H2 overview, 1+=full tree). |
| `parent_node_id` | string | No | - | Return only the subtree under this node ID. |

For large notes (total_tokens > 3000): use `depth=0` first to get top-level sections, then drill into specific sections with `parent_node_id`.

---

### `memex_get_nodes`

Read note sections by node IDs. Get node IDs from `memex_get_page_indices`. Accepts 1 or more IDs — use for single and batch reads.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `node_ids` | string[] | Yes | List of Node UUIDs. |

Returns section titles, content text, and note IDs. Falls back to individual lookups if batch endpoint is unavailable.

---

### `memex_read_note`

Read full note content. Only when total_tokens < 500. Otherwise use `memex_get_page_indices` + `memex_get_nodes`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note to retrieve. |

---

## Note Management Tools

### `memex_add_note`

Add a note to the Memex knowledge base. The vault parameter is optional and defaults to `config.write_vault`. Use `memex_active_vault` to check the current write vault or `memex_list_vaults` to enumerate.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | string | Yes | - | The title of the note. |
| `markdown_content` | string | Yes | - | Note content in markdown. Use `memex_get_template` to get the expected structure. |
| `description` | string | Yes | - | Summary of note content (max 250 words). Cover context/intent and key insights. |
| `author` | string | Yes | - | Name of the model authoring this note. |
| `tags` | string[] | Yes | - | Tags for easier retrieval. |
| `supporting_files` | string[] | No | - | Absolute paths to supporting files (images, CSVs). |
| `vault_id` | string | No | `config.write_vault` | UUID or name of the vault to add the note to. Defaults to resolved write vault from config. |
| `note_key` | string | No | - | Unique stable key for incremental updates. |
| `background` | bool | No | `false` | Queue ingestion in background. |
| `user_notes` | string | No | - | Optional user-provided context or commentary to include in the note. |
| `date` | string | No | - | Note date in ISO 8601 format (e.g. `2026-03-27`). Defaults to now. |
| `template` | string | No | - | Template slug used to create this note (e.g. `"general_note"`). |

On success, returns the note ID. If similar notes already exist, includes overlap warnings with note titles, similarity percentages, and IDs.

---

### `memex_set_note_status`

Set note lifecycle status: active, superseded, or appended. When superseded, all memory units are marked stale. Optionally link to the replacing/parent note.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `note_id` | string | Yes | - | The UUID of the note. |
| `status` | string | Yes | - | New status: `active`, `superseded`, or `appended`. |
| `linked_note_id` | string | No | - | UUID of the note that supersedes or contains this one. |

---

### `memex_rename_note`

Rename a note. Updates title in metadata, page index, and doc_metadata.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note. |
| `new_title` | string | Yes | The new title for the note. |

---

### `memex_update_user_notes`

Update user_notes on an existing note and reprocess into the memory graph. Pass `null` to delete all user annotations. Old user_notes memory units are deleted and new ones extracted.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | Note UUID. |
| `user_notes` | string | No | New user_notes text, or `null` to delete all annotations. |

---

### `memex_get_template`

Retrieve a markdown template for note creation. Use the returned template as the structure for `memex_add_note`. Use `memex_list_templates` to discover available templates.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | Yes | Template slug. Use `memex_list_templates` to discover available templates. Built-in types include: `technical_brief`, `general_note`, `architectural_decision_record`, `request_for_comments`, `quick_note`. |

---

### `memex_list_templates`

List all available note templates with metadata (slug, name, description, source scope). Templates are discovered across three layers: built-in, global (`{filestore_root}/templates/`), and project-local (`.memex/templates/`).

No parameters.

Returns a formatted list of templates with slug, source scope, display name, and description.

---

### `memex_register_template`

Register a new note template from inline content. Creates a template in the global scope. To delete a template, use the CLI: `memex note template delete <slug>`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `slug` | string | Yes | - | Template identifier (e.g. `sprint_retro`). |
| `template` | string | Yes | - | Markdown template content. Should include YAML frontmatter. |
| `name` | string | No | - | Human-readable template name. |
| `description` | string | No | - | Short description of the template. |

Returns confirmation with the registered slug, display name, and scope.

---

## Asset & Resource Tools

### `memex_list_assets`

List all file assets (images, PDFs, etc.) attached to a note.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | The UUID of the note. |

Returns filenames, MIME types, and paths. Use paths with `memex_get_resources` to retrieve file content.

---

### `memex_get_resources`

Retrieve 1+ file resources (images, audio, documents) by path. Get paths from `memex_list_assets`. Accepts a single path or a list.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `paths` | string[] | Yes | Resource path(s). |
| `vault_id` | string | No | Vault UUID or name. Defaults to `config.write_vault`. |

Returns `Image`, `Audio`, `File`, or error strings for each path. Per-item failures don't block other resources.

---

### `memex_add_assets`

Add one or more file assets to an existing note. Provide local file paths.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | Note UUID. |
| `file_paths` | string[] | Yes | Absolute paths to asset files to attach. |
| `vault_id` | string | No | Vault UUID or name. Omit to use config defaults. |

Returns the list of added assets, skipped duplicates, and updated asset count.

---

### `memex_delete_assets`

Delete one or more asset files from an existing note. Get paths from `memex_list_assets`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | Yes | Note UUID. |
| `asset_paths` | string[] | Yes | Asset path(s) to delete (from `memex_list_assets`). |
| `vault_id` | string | No | Vault UUID or name. Omit to use config defaults. |

Returns the list of deleted paths, not-found paths, and updated asset count.

---

## Entity Tools

### `memex_list_entities`

List or search entities in the knowledge graph. Without a query, returns top entities by relevance.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | No | - | Search term to filter by name. |
| `limit` | int | No | `20` | Maximum entities to return. |
| `vault_id` | string | No | - | Vault UUID or name to filter by. |
| `entity_type` | string | No | - | Filter by entity type: `Person`, `Organization`, `Location`, `Concept`, `Technology`, `File`, `Misc`. |

---

### `memex_get_entities`

Get entity details (name, type, mention count) for 1+ entities by UUID. Use after `memex_list_entities` to get full details.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_ids` | string[] | Yes | List of Entity UUIDs. |

Returns entity name, ID, type, mention count, and vault. Falls back to individual lookups if batch endpoint is unavailable.

---

### `memex_get_entity_mentions`

Get facts, observations, and events that mention an entity. Each mention links to its source note, revealing cross-note connections.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `entity_id` | string | Yes | - | The UUID of the entity. |
| `limit` | int | No | `10` | Maximum mentions to return. |

---

### `memex_get_entity_cooccurrences`

Find entities that frequently appear alongside a given entity — the fastest way to map relationships and discover connected concepts. Returns entity names, types, and co-occurrence counts inline (no follow-up calls needed).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `entity_id` | string | Yes | - | The UUID of the entity. |
| `limit` | int | No | `10` | Max co-occurring entities to return. |

---

## Memory Unit Tools

### `memex_get_memory_units`

Batch lookup of memory units by ID. Includes contradiction links and supersession info.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `unit_ids` | string[] | Yes | List of memory unit UUIDs. |

Returns unit text, type, confidence, note ID, and supersession context for each unit.

---

## KV Store Tools

### `memex_kv_write`

Write a fact to the key-value store. Generates an embedding for semantic search. Use for storing structured preferences, settings, or facts. Key should be a short, namespaced identifier (e.g. `"tool:python:pkg_mgr"`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `value` | string | Yes | - | The fact or preference text to store. |
| `key` | string | Yes | - | Namespaced key, e.g. `"tool:python:pkg_mgr"`. |
| `vault_id` | string | No | `null` (global) | Vault UUID or name. `null` = global (available in all vaults). |

Returns the stored key-value pair and scope.

---

### `memex_kv_get`

Get a fact by exact key from the KV store.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `key` | string | Yes | - | Exact key to look up. |
| `vault_id` | string | No | - | Vault UUID or name. Checks vault-specific first, then global. |

Returns the key, value, scope, and last updated timestamp. Returns "Key not found" if the key does not exist.

---

### `memex_kv_search`

Fuzzy search facts in the KV store by semantic similarity. Returns the closest matching entries.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query text. |
| `vault_id` | string | No | - | Vault UUID or name. `null` = search global entries only. |
| `limit` | int | No | `5` | Maximum results to return. |

Returns matching facts with keys, values, scopes, and timestamps.

---

### `memex_kv_list`

List all facts in the KV store. Without `vault_id`, returns global entries only.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `vault_id` | string | No | - | Vault UUID or name. `null` = global entries only; with vault = both global and vault-scoped. |

Returns all KV entries with keys, values, scopes, and timestamps.

---

## Note Browsing Tools

### `memex_list_notes`

List notes with optional date filters. Use `after`/`before` for temporal queries like "documents from 2026".

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `vault_id` | string | No | from config | Vault UUID or name. Omit to use config defaults. |
| `after` | string | No | - | Only notes on/after this date (ISO 8601, e.g. `2026-01-01`). |
| `before` | string | No | - | Only notes on/before this date (ISO 8601, e.g. `2026-12-31`). |
| `limit` | int | No | `50` | Max notes to return. |
| `template` | string | No | - | Filter by template slug (e.g. `"general_note"`). |
| `tags` | string[] | No | - | Filter by tags (AND semantics). Only notes containing all specified tags. |
| `status` | string | No | - | Filter by note lifecycle status (e.g. `"active"`, `"archived"`). |

Returns note titles, IDs, creation dates, publish dates, and vault IDs.

---

## Vault Tools

### `memex_active_vault` [DEPRECATED]

> **Deprecated.** Use `memex_list_vaults` instead, which now includes an `is_active` flag on each vault. This tool will be removed in a future version.

Retrieve the currently active vault information. No parameters.

Returns the active vault name and ID.

---

### `memex_list_vaults`

List all available vaults. Each vault includes an `is_active` flag indicating the current writer vault. No parameters.

Returns vault names, IDs, descriptions, and active status.

---

### `memex_get_vault_summary`

Get the current summary for a vault. Returns topics, stats, and a natural language overview of vault contents. Use this to orient yourself in a vault.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `vault_id` | string | No | Vault UUID or name. Omit to use the active vault. |

Returns summary text, topics, stats, version, notes incorporated, and timestamps.

---

### `memex_recent_notes`

Browse recent notes. Defaults to all vaults. Filter by vault names/UUIDs and optional date range. Not recommended for discovery — use `memex_memory_search` or `memex_note_search` instead.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | int | No | `20` | Maximum notes to return. |
| `vault_ids` | string[] | No | - | Vault UUIDs or names. Omit for all vaults. |
| `after` | string | No | - | Only notes on/after this date (ISO 8601). |
| `before` | string | No | - | Only notes on/before this date (ISO 8601). |
| `template` | string | No | - | Filter by template slug (e.g. `"general_note"`). |

---

## Lineage Tools

### `memex_get_lineage`

Trace the provenance chain of an entity. Upstream: mental_model → observation → memory_unit → note. Downstream: note → memory_unit → observation → mental_model.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `entity_type` | string | Yes | - | Entity type: `mental_model`, `observation`, `memory_unit`, or `note`. |
| `entity_id` | string | Yes | - | UUID of the entity. |
| `direction` | string | No | `upstream` | Traversal direction: `upstream`, `downstream`, or `both`. |
| `depth` | int | No | `3` | Max recursion depth. |
| `limit` | int | No | `5` | Max children per node. |

Returns a tree structure showing the provenance chain with entity types, IDs, labels, and children at each level.
