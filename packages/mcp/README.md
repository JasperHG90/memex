# Memex MCP Server (`memex-mcp`)

A Model Context Protocol (MCP) server that exposes Memex capabilities to AI assistants like Claude Desktop and IDE agents.

## Features

Enables your AI assistant to:
- **Search** your personal knowledge base (`memex_search`).
- **Read** full notes and documents (`memex_read_note`).
- **Trace** the origin of facts (`memex_get_lineage`).
- **Save** important context back to Memex (`memex_add_note`).

## Usage

The MCP server is usually run as a subcommand of the main CLI.

1. **Start the Core Server** (required):
```bash
memex server start
```

2. **Run the MCP Server**:
```bash
memex mcp run
```

Or via `uv` for direct package execution:
```bash
uv run memex mcp run
```

## Documentation

- [Using MCP](../../docs/how-to/using-mcp.md)
- [MCP Tools Reference](../../docs/reference/mcp-tools.md)
