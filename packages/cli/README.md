# Memex CLI (`memex-cli`)

The command-line interface for Memex, your personal AI knowledge vault.

## Overview

The `memex` CLI allows you to:
- **Ingest** documents, URLs, and folders into your knowledge base.
- **Search** memories and notes using semantic and keyword strategies.
- **Manage** vaults, entities, and memories.
- **Run** the Memex server, dashboard, and MCP server.
- **Administer** the database with Alembic migrations.

## Installation

```bash
uv tool install "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
```

## Quick Start

```bash
# Initialize configuration
memex config init

# Start server (required for all operations)
memex server start

# Ingest a webpage
memex note add --url "https://example.com"

# Search for answers
memex memory search "What are the key points?"
```

## Command Groups

| Command | Description |
|:--------|:------------|
| `memex memory` | Add, search, delete, reflect on memories; trace lineage. |
| `memex note` | List, search, view, and delete notes; read page index and nodes. |
| `memex entity` | List, view, delete entities; inspect mentions and related entities. |
| `memex vault` | Create, list, and delete vaults. |
| `memex server` | Start, stop, and check status of the API server. |
| `memex dashboard` | Start, stop, and check status of the React dashboard. |
| `memex mcp` | Run the MCP server (stdio or SSE transport). |
| `memex stats` | View system statistics and token usage. |
| `memex config` | Show current configuration or initialize a config file. |
| `memex db` | Database migrations via Alembic (upgrade, downgrade, history, stamp, revision). |
| `memex setup claude-code` | Configure Claude Code integration (MCP, hooks, skills). |

## Global Options

| Flag | Description |
|:-----|:------------|
| `--server-url URL` | Override the Memex server URL. |
| `--config PATH` | Path to a config file. |
| `--verbose` / `-v` | Enable verbose output. |
| `--version` | Show version and exit. |

## Documentation

For a complete command reference, see the [CLI Reference](../../docs/reference/cli-commands.md).
