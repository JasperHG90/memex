# How to Use the MCP Server

This guide shows you how to connect Memex to AI assistants via the Model Context Protocol (MCP), including Claude Desktop, Claude Code, Cursor, and other MCP-compatible clients.

## Prerequisites

* Memex installed with the MCP extra (`uv tool install git+https://github.com/JasperHG90/memex.git[mcp]`)
* A running Memex server (`memex server start`)
* An MCP-compatible AI client

## Available MCP Tools

The Memex MCP server exposes these tools to connected AI clients:

| Tool | Purpose |
| :--- | :--- |
| `memex_memory_search` | Search memory units (facts, events, observations) via TEMPR |
| `memex_note_search` | Search source notes via hybrid retrieval (semantic + BM25 + graph) |
| `memex_add_note` | Save new knowledge to Memex |
| `memex_set_note_status` | Set note lifecycle status (active, superseded, appended) |
| `memex_rename_note` | Rename a note (updates title in metadata and page index) |
| `memex_read_note` | Read full note content (fallback — prefer `get_page_index` + `get_node`) |
| `memex_get_page_index` | Get the table of contents for a note |
| `memex_get_node` | Retrieve a specific section of a note by node ID |
| `memex_get_lineage` | Trace provenance of a memory unit back to its source |
| `memex_reflect` | Manually trigger reflection on an entity |
| `memex_list_assets` / `memex_get_resource` | Retrieve attached files (images, PDFs) |
| `memex_get_template` | Get markdown templates for structured notes |
| `memex_batch_ingest` / `memex_get_batch_status` | Bulk file ingestion |
| `memex_ingest_url` | Ingest content from a URL |
| `memex_list_vaults` / `memex_active_vault` | Vault management |
| `memex_list_entities` / `memex_get_entity` | Browse the entity graph |
| `memex_get_entity_mentions` / `memex_get_entity_cooccurrences` | Entity relationships |
| `memex_list_notes` | List notes (not recommended for discovery) |
| `memex_get_memory_unit` | Retrieve a specific memory unit by UUID |
| `memex_get_memory_units` | Batch lookup of memory units with contradiction context |
| `memex_get_note_metadata` | Quick metadata check (title, tags, dates) — ~50 tokens |

## Instructions

### Configure for Claude Desktop

Add the following to your `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_SERVER__ACTIVE_VAULT": "global"
      }
    }
  }
}
```

### Configure for Claude Code

Run the automated setup command:

```bash
memex setup claude-code
```

This generates the MCP configuration, CLAUDE.md integration instructions, and lifecycle hooks (session start, compaction, commit) in your project directory.

To manually configure, add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_SERVER__ACTIVE_VAULT": "my-project"
      }
    }
  }
}
```

### Configure for Cursor

Add to your Cursor MCP settings (`.cursor/mcp.json` in your project root):

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_SERVER__ACTIVE_VAULT": "global"
      }
    }
  }
}
```

### Use SSE Transport (Remote Server)

For remote or shared deployments, run the MCP server in SSE mode instead of stdio:

```bash
memex mcp run --transport sse --host 0.0.0.0 --port 8000
```

Then configure your client to connect via SSE instead of spawning a subprocess:

```json
{
  "mcpServers": {
    "memex": {
      "url": "http://your-server:8000/sse"
    }
  }
}
```

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| "memex_mcp is not installed" | Missing MCP extra | Run `uv tool install git+https://github.com/JasperHG90/memex.git[mcp]` |
| Tools not appearing in client | Config file in wrong location | Check the path for your OS (see above) |
| "Connection refused" errors | Memex server not running | Start with `memex server start` |
| Wrong vault in results | `MEMEX_SERVER__ACTIVE_VAULT` not set | Add the env var to your MCP config |
| Slow tool responses | Large result sets | Reduce `limit` parameter or set `token_budget` |
| "No results found" | Empty vault or unprocessed notes | Check `memex note list` and wait for extraction to complete |

## Verification

After configuring, verify the connection by asking your AI assistant:

> "List all available vaults in Memex"

The assistant should call `memex_list_vaults` and return your vault names. If this works, all MCP tools are accessible.

## Best Practices for AI Agents

- **Search before answering**: Use `memex_memory_search` to ground responses in stored knowledge.
- **Use lineage for sources**: When asked for citations, call `memex_get_lineage` to trace facts back to source documents.
- **Use templates for consistency**: Call `memex_get_template` before saving structured notes (ADRs, tech briefs).
- **Check the active vault**: Call `memex_active_vault` before writing to confirm the target vault.
- **Prefer page index over full reads**: Use `memex_get_page_index` then `memex_get_node` instead of `memex_read_note` for large notes.

## See Also

* [Configuring Memex](configure-memex.md) — environment variables and YAML settings
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for each tool
* [Document Search vs. Memory Search](doc-search-vs-memory-search.md) — choosing between `memex_memory_search` and `memex_note_search`
