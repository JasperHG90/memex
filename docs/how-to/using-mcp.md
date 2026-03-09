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
| `memex_read_note` | Read full note content (fallback — prefer `memex_get_page_indices` + `memex_get_nodes`) |
| `memex_get_page_indices` | Get the table of contents for 1+ notes |
| `memex_get_nodes` | Retrieve note sections by node IDs (batch) |
| `memex_get_notes_metadata` | Quick metadata check for 1+ notes (title, tags, dates) — ~50 tokens each |
| `memex_list_assets` / `memex_get_resources` | Retrieve attached files (images, PDFs) |
| `memex_get_template` | Get markdown templates for structured notes |
| `memex_list_vaults` / `memex_active_vault` | Vault management |
| `memex_recent_notes` | Browse recent notes (not for discovery) |
| `memex_list_entities` / `memex_get_entities` | Browse the entity graph |
| `memex_get_entity_mentions` / `memex_get_entity_cooccurrences` | Entity relationships |
| `memex_get_memory_units` | Batch lookup of memory units with contradiction context |

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
- **Use templates for consistency**: Call `memex_get_template` before saving structured notes (ADRs, tech briefs).
- **Check the active vault**: Call `memex_active_vault` before writing to confirm the target vault.
- **Prefer page index over full reads**: Use `memex_get_page_indices` then `memex_get_nodes` instead of `memex_read_note` for large notes.

## See Also

* [Configuring Memex](configure-memex.md) — environment variables and YAML settings
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for each tool
* [Document Search vs. Memory Search](doc-search-vs-memory-search.md) — choosing between `memex_memory_search` and `memex_note_search`
