# Migrate MCP integrations from v0.0.7 to v0.0.8

This guide covers the breaking changes to the MCP tool surface between Memex v0.0.7 and v0.0.8. If you have `.mcp.json` configs, `CLAUDE.md` instructions, OpenClaw configurations, or scripts that reference Memex MCP tools, follow this guide to update them.

## Summary of changes

| Change | Impact | Effort |
|--------|--------|--------|
| Tool surface reduced (28 → 21) | 6 tools removed, 4 batched, 3 new tools added | Medium — update all tool references |
| Tool names pluralized | 2 tools renamed | Low — find and replace |
| `vault_id` / `vault_ids` now required | All vault-scoped tools require explicit vault | Medium — find your vault ID, update all calls |
| Co-occurrences endpoint pluralized | 1 tool renamed | Low — find and replace |

## Step 1: Update removed tools

Ten tools from v0.0.7 no longer exist as-is. Four were replaced by batch equivalents; six were removed entirely.

### Replaced by batch equivalents

These singular tools were replaced by batch versions that accept lists of IDs. Update your calls to pass a single-element list where needed.

| Removed tool | Replacement | Notes |
|---|---|---|
| `memex_get_node` | `memex_get_nodes` | Pass `node_ids: ["<id>"]` instead of `node_id: "<id>"` |
| `memex_get_note_metadata` | `memex_get_notes_metadata` | Pass `note_ids: ["<id>"]` instead of `note_id: "<id>"` |
| `memex_get_memory_unit` | `memex_get_memory_units` | Pass `unit_ids: ["<id>"]` instead of `unit_id: "<id>"` |
| `memex_get_entity` | `memex_get_entities` | Pass `entity_ids: ["<id>"]` instead of `entity_id: "<id>"` |

### Removed entirely (no replacement)

These tools were removed because they are not needed by AI assistants in typical workflows. Use the REST API or CLI directly if you still need this functionality.

| Removed tool | Alternative |
|---|---|
| `memex_reflect` | Reflection runs automatically in the background. Trigger manually via REST API: `POST /api/v1/reflect` |
| `memex_get_lineage` | Use REST API: `GET /api/v1/lineage/{unit_id}` |
| `memex_batch_ingest` | Use REST API: `POST /api/v1/ingest/batch` or CLI: `memex note import` |
| `memex_get_batch_status` | Use REST API: `GET /api/v1/ingest/batch/{job_id}` |
| `memex_migrate_note` | Use CLI: `memex note migrate` |
| `memex_ingest_url` | Use REST API: `POST /api/v1/ingest/url` or CLI: `memex note ingest-url` |

## Step 2: Update renamed tools

Three tools were renamed for consistency (pluralized to match batch semantics).

| Old name | New name |
|---|---|
| `memex_get_page_index` | `memex_get_page_indices` |
| `memex_get_resource` | `memex_get_resources` |

> `memex_list_notes` still exists but a new `memex_recent_notes` tool was added for time-sorted access. Check the docs for which one fits your use case.

**Find and replace** these names in your `CLAUDE.md`, `.mcp.json` instructions, and any scripts or prompts that reference them.

## Step 3: Add `vault_id` to all calls

In v0.0.7, `vault_id` was optional — tools defaulted to the active vault. In v0.0.8, **`vault_id` (or `vault_ids`) is required** on all vault-scoped tools.

### Find your vault ID

```bash
# Via CLI
memex vault list

# Via REST API
curl -s http://localhost:8000/api/v1/vaults | jq

# Via MCP (still works without vault_id)
# Use the memex_active_vault or memex_list_vaults tools
```

Example response:
```json
{"id": "ac9b6a45-...", "name": "global"}
```

### Update your calls

**Before (v0.0.7):**
```json
{
  "tool": "memex_memory_search",
  "arguments": {
    "query": "authentication patterns"
  }
}
```

**After (v0.0.8):**
```json
{
  "tool": "memex_memory_search",
  "arguments": {
    "query": "authentication patterns",
    "vault_ids": ["global"]
  }
}
```

> You can pass vault names (e.g. `"global"`) or UUIDs. Names are resolved server-side.

### Tools that require `vault_id` (singular string)

- `memex_read_note`
- `memex_add_note`
- `memex_get_resources`
- `memex_list_entities`
- `memex_recent_notes`

### Tools that require `vault_ids` (list of strings)

- `memex_memory_search`
- `memex_note_search`

### Tools unaffected (no vault parameter)

- `memex_active_vault`
- `memex_list_vaults`
- `memex_get_template`
- `memex_get_page_indices`
- `memex_get_nodes`
- `memex_get_notes_metadata`
- `memex_get_entities`
- `memex_get_memory_units`
- `memex_get_entity_mentions`
- `memex_get_entity_cooccurrences`
- `memex_list_assets`
- `memex_set_note_status`
- `memex_rename_note`

## Step 4: Update CLAUDE.md and MCP instructions

If your `CLAUDE.md` or MCP system prompts reference tool names, update them to match. Common patterns to fix:

```diff
- Use `memex_get_page_index` to get the table of contents
+ Use `memex_get_page_indices` to get the table of contents

- Use `memex_get_node` to read a section
+ Use `memex_get_nodes` to read sections (pass a list of node IDs)

- Use `memex_get_note_metadata` to check relevance
+ Use `memex_get_notes_metadata` to check relevance (pass a list of note IDs)

- PROHIBITED: `memex_list_notes` for discovery
+ PROHIBITED: `memex_list_notes` / `memex_recent_notes` for discovery
```

Also update the retrieval workflow to pass `vault_ids`:

```diff
  memex_memory_search — atomic facts across the knowledge graph.
+ Always pass vault_ids, e.g. vault_ids: ["global"]
```

## Step 5: Update `.mcp.json` (if using custom instructions)

If your `.mcp.json` includes tool-level configuration or descriptions, update the tool names. The server name and connection settings remain unchanged:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/memex", "memex", "mcp", "run"],
      "env": {
        "MEMEX_SERVER_URL": "http://localhost:8000"
      }
    }
  }
}
```

No changes needed to the server configuration itself — only to tool references in prompts and scripts.

## Complete tool mapping (v0.0.7 → v0.0.8)

| v0.0.7 tool | v0.0.8 tool | Change |
|---|---|---|
| `memex_memory_search` | `memex_memory_search` | `vault_ids` now required |
| `memex_note_search` | `memex_note_search` | `vault_ids` now required |
| `memex_get_note_metadata` | `memex_get_notes_metadata` | Batched (accepts list of IDs) |
| `memex_get_page_index` | `memex_get_page_indices` | Renamed |
| `memex_get_node` | `memex_get_nodes` | Batched (accepts list of IDs) |
| `memex_read_note` | `memex_read_note` | `vault_id` now required |
| `memex_add_note` | `memex_add_note` | `vault_id` now required |
| `memex_get_template` | `memex_get_template` | Unchanged |
| `memex_active_vault` | `memex_active_vault` | Unchanged |
| `memex_list_vaults` | `memex_list_vaults` | Unchanged |
| `memex_list_notes` | `memex_list_notes` | Still exists |
| `memex_list_assets` | `memex_list_assets` | Unchanged |
| `memex_get_resource` | `memex_get_resources` | Renamed |
| `memex_list_entities` | `memex_list_entities` | `vault_id` now required |
| `memex_get_entity` | `memex_get_entities` | Batched (accepts list of IDs) |
| `memex_get_entity_mentions` | `memex_get_entity_mentions` | Unchanged |
| `memex_get_entity_cooccurrences` | `memex_get_entity_cooccurrences` | Unchanged |
| `memex_get_memory_unit` | `memex_get_memory_units` | Batched (accepts list of IDs) |
| `memex_get_lineage` | *(removed)* | Use REST API |
| `memex_reflect` | *(removed)* | Runs automatically; use REST API |
| `memex_batch_ingest` | *(removed)* | Use REST API or CLI |
| `memex_get_batch_status` | *(removed)* | Use REST API |
| `memex_migrate_note` | *(removed)* | Use CLI |
| `memex_ingest_url` | *(removed)* | Use REST API or CLI |
| *(new)* | `memex_recent_notes` | Time-sorted note listing |
| *(new)* | `memex_set_note_status` | Archive/restore notes |
| *(new)* | `memex_rename_note` | Rename existing notes |

## Troubleshooting

### "vault_ids must be a list of strings"

You're passing `vault_ids` as a string instead of a list. Use `["global"]` not `"global"`.

### "Tool not found: memex_get_page_index"

The tool was renamed to `memex_get_page_indices`. Update your tool reference.

### "Tool not found: memex_get_memory_unit"

Renamed to `memex_get_memory_units` (plural). Pass `unit_ids: ["<id>"]`.

### Dashboard shows no data after upgrade

If the dashboard loads but shows empty data, check if your nginx container has stale Docker DNS entries. Restart the dashboard container:

```bash
docker compose restart dashboard
```

If the API container was also restarted, Docker may have assigned it a new internal IP. Restarting nginx resolves the stale DNS.
