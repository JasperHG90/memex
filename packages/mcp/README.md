# Memex MCP Server (`memex-mcp`)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes 31 Memex tools to AI assistants like Claude Desktop, Claude Code, and other MCP-compatible clients. Built with [FastMCP](https://github.com/jlowin/fastmcp). Uses progressive disclosure by default — clients see 3 discovery meta-tools instead of 31 schemas upfront.

## Features

Enables your AI assistant to:
- **Search** your knowledge base — memory units (`memex_memory_search`) and source notes (`memex_note_search`).
- **Read** notes hierarchically — table of contents (`memex_get_page_indices`) then sections (`memex_get_nodes`).
- **Save** important context back to Memex (`memex_add_note`).
- **Explore** the knowledge graph — entities, mentions, and co-occurrences.
- **Manage** vaults and note lifecycle.

## Progressive Disclosure

By default, `tools/list` returns 3 discovery meta-tools instead of all 31 tool schemas:

1. **`memex_tags`** — browse tool categories and counts
2. **`memex_search`** — find tools by keyword (BM25), optionally filtered by tag
3. **`memex_get_schema`** — get parameter details for specific tools

Real tools remain directly callable by name via `tools/call`. Set `MEMEX_MCP_PROGRESSIVE_DISCLOSURE=false` to disable and expose all 31 tools directly.

## Tool Categories

| Tag | Tools | Count |
|:----|:------|------:|
| `search` | `memex_memory_search`, `memex_note_search`, `memex_find_note` | 3 |
| `read` | `memex_get_page_indices`, `memex_get_nodes`, `memex_get_notes_metadata`, `memex_read_note` | 4 |
| `write` | `memex_add_note`, `memex_set_note_status`, `memex_rename_note`, `memex_get_template`, `memex_list_templates`, `memex_register_template` | 6 |
| `browse` | `memex_list_notes`, `memex_recent_notes`, `memex_list_vaults`, `memex_active_vault` | 4 |
| `assets` | `memex_list_assets`, `memex_get_resources`, `memex_add_assets`, `memex_delete_assets` | 4 |
| `entities` | `memex_list_entities`, `memex_get_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences` | 4 |
| `storage` | `memex_kv_write`, `memex_kv_get`, `memex_kv_search`, `memex_kv_list`, `memex_get_memory_units`, `memex_get_lineage` | 6 |
| **Total** | | **31** |

## Usage

The MCP server requires the Core server to be running.

### 1. Start the Core Server

```bash
memex server start
```

### 2. Run the MCP Server

```bash
# stdio transport (default — for Claude Code, IDE integrations)
memex mcp run

# HTTP transport (for remote/web clients, Docker)
memex mcp run --transport http --port 8080

# SSE transport (legacy)
memex mcp run --transport sse --port 8080
```

A slim Docker image is available at `docker/mcp/Dockerfile` — it includes only `memex-common`, `memex-mcp`, and `memex-cli` (no core/ML dependencies). Connects to an external Memex API server.

### Claude Code Integration

Run the automated setup to configure MCP, hooks, and skills:

```bash
memex setup claude-code
```

Or configure manually in your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"]
    }
  }
}
```

## Documentation

- [Using MCP](../../docs/how-to/using-mcp.md)
- [MCP Tools Reference](../../docs/reference/mcp-tools.md)
