# Memex MCP Server (`memex-mcp`)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes 26 Memex tools to AI assistants like Claude Desktop, Claude Code, and other MCP-compatible clients. Built with [FastMCP](https://github.com/jlowin/fastmcp).

## Features

Enables your AI assistant to:
- **Search** your knowledge base — memory units (`memex_memory_search`) and source notes (`memex_note_search`).
- **Read** notes hierarchically — table of contents (`memex_get_page_indices`) then sections (`memex_get_nodes`).
- **Save** important context back to Memex (`memex_add_note`).
- **Explore** the knowledge graph — entities, mentions, and co-occurrences.
- **Manage** vaults and note lifecycle.

## Tool Categories

| Category | Tools | Count |
|:---------|:------|------:|
| Search | `memex_memory_search`, `memex_note_search`, `memex_find_note` | 3 |
| Note Reading | `memex_get_page_indices`, `memex_get_nodes`, `memex_get_notes_metadata`, `memex_read_note` | 4 |
| Note Management | `memex_add_note`, `memex_set_note_status`, `memex_rename_note`, `memex_get_template` | 4 |
| Note Browsing | `memex_list_notes`, `memex_recent_notes` | 2 |
| Assets & Resources | `memex_list_assets`, `memex_get_resources` | 2 |
| Entities | `memex_list_entities`, `memex_get_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences` | 4 |
| Memory Units | `memex_get_memory_units` | 1 |
| KV Store | `memex_kv_write`, `memex_kv_get`, `memex_kv_search`, `memex_kv_list` | 4 |
| Vaults | `memex_active_vault`, `memex_list_vaults` | 2 |
| **Total** | | **26** |

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
