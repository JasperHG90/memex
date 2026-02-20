# Using the MCP Server

Memex implements the Model Context Protocol (MCP), allowing AI assistants (like Claude Desktop or IDE agents) to interact with your knowledge base.

## Capabilities

The MCP server exposes tools that allow an LLM to:

1.  **Search**: `memex_search` (Search memories and documents).
2.  **Read**: `memex_read_note` (Read full note content).
3.  **Explore**: `memex_get_lineage` (Trace the origin of facts).
4.  **Assets**: `memex_list_assets` & `memex_get_resource` (Retrieve images, PDFs).
5.  **Templates**: `memex_get_template` (Standardized note formats like ADR or Tech Brief).
6.  **Reflect**: `memex_reflect` (Manually trigger consolidation).
7.  **Save**: `memex_add_note` (Capture new information).
8.  **Batch**: `memex_batch_ingest` & `memex_get_batch_status` (Background ingestion).

## Configuration (Claude Desktop)

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": [
        "run",
        "memex",
        "mcp",
        "run"
      ],
      "env": {
        "MEMEX_SERVER__ACTIVE_VAULT": "global"
      }
    }
  }
}
```

## Best Practices for Agents

When using Memex via an Agent:

- **Ground Truth First**: Before answering a query, always use `memex_search` to find relevant context.
- **Traceability**: If the user asks for sources, use `memex_get_lineage` to show the path from raw Document to extracted Fact.
- **Structured Notes**: When saving new knowledge, use `memex_get_template` to ensure the note follows project standards.
- **Asset Awareness**: If a search result indicates attached files, use `memex_list_assets` and `memex_get_resource` to "see" images or read PDFs.
- **Idempotency**: Use `memex_active_vault` to verify which vault you are currently writing to.
