# Memex MCP Server (`memex-mcp`)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes 26 Memex tools to AI assistants like Claude Desktop, Claude Code, and other MCP-compatible clients. Built with [FastMCP](https://github.com/jlowin/fastmcp).

## Features

Enables your AI assistant to:
- **Search** your knowledge base — memory units (`memex_memory_search`) and source notes (`memex_note_search`).
- **Read** notes hierarchically — table of contents (`memex_get_page_index`) then sections (`memex_get_node`).
- **Trace** the origin of facts via provenance chains (`memex_get_lineage`).
- **Save** important context back to Memex (`memex_add_note`).
- **Explore** the knowledge graph — entities, mentions, and co-occurrences.
- **Manage** vaults, trigger reflection, and ingest URLs or file batches.

## Tool Categories

| Category | Tools | Count |
|:---------|:------|------:|
| Search | `memex_memory_search`, `memex_note_search` | 2 |
| Note Reading | `memex_get_page_index`, `memex_get_node`, `memex_read_note` | 3 |
| Note Management | `memex_add_note`, `memex_get_template` | 2 |
| Assets & Resources | `memex_list_assets`, `memex_get_resource` | 2 |
| Entities | `memex_list_entities`, `memex_get_entity`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences` | 4 |
| Memory Units | `memex_get_memory_unit` | 1 |
| Lineage | `memex_get_lineage` | 1 |
| Reflection | `memex_reflect` | 1 |
| Ingestion | `memex_ingest_url`, `memex_batch_ingest`, `memex_get_batch_status` | 3 |
| Vaults | `memex_active_vault`, `memex_list_vaults`, `memex_list_notes` | 3 |
| **Total** | | **22** |

Note: The server registers additional prompt and resource capabilities beyond the 22 tool functions, bringing the total MCP surface to 26 registered items.

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

# SSE transport (for remote/web clients)
memex mcp run --transport sse --port 8080
```

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
