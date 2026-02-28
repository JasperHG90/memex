# Memex

Memex is a long-term memory system designed to give LLMs persistent, evolving knowledge. It captures unstructured data (notes, docs, chats), extracts structured facts, and synthesizes high-level mental models over time.

## Requirements

1. UV
2. Postgres with pgvector

## 🚀 Quick Start

### 1. Install
Requires Python 3.12+ and `uv`.

```bash
uv tool install git+https://github.com/JasperHG90/memex.git[mcp,server]
```

It's easiest to just alias the `uv tool` command: `alias memex="uv tool run --from memex-cli memex"`

> The dashboard is a separate React+Vite application. See [Using the Dashboard](./docs/tutorials/using-the-dashboard.md) for setup instructions.

### 2. Initialize
Sets up your local storage and configuration.

```bash
memex config init
```

### 3. Start the Server
Memex requires a running API server for all operations.

```bash
# In a separate terminal
memex server start -d
```

### 4. Ingest
Feed it knowledge.

```bash
# Isolate notes with vaults
memex vault create notes --description "Notes about things"

# Inline note
memex memory add -v notes "Memex provides long-term memory that evolves."

# Capture a webpage
# Goes to the 'global' vault
memex memory add --url "https://docs.python.org/3/tutorial/"

# Point it to local files
# Supports: MD, PDF, docx, xlsx, outlook, pptx
memex memory add --file /path/to/file.md --vault notes
```

### 5. Search
Ask questions.

```bash
memex memory search "How does Python handle memory management?"
```

## See it in action

### Searching notes

![](assets/memex_cli_docs.gif)

### Searching memory

![](assets/memex_cli_memory.gif)

### Entity management

![](assets/memex_cli_entities.gif)

### Dashboard

![](assets/memex_dashboard.gif)

## 📚 Documentation

Comprehensive guides and references are available in [`docs/`](./docs).

### Basics
- [Getting Started](./docs/tutorials/getting-started.md)
- [Configuration](./docs/how-to/configure-memex.md)
- [Using the Dashboard](./docs/tutorials/using-the-dashboard.md)

### Key Features
- [Hindsight Framework](./docs/explanation/hindsight-framework.md): How Memex "thinks" and remembers.
- [Extraction Pipeline](./docs/explanation/extraction-pipeline.md): Understanding fact extraction.
- [Retrieval Strategies](./docs/explanation/retrieval-strategies.md): The TEMPR system — five search strategies fused via RRF.
- [Doc Search vs Memory Search](./docs/how-to/doc-search-vs-memory-search.md): Choosing the right retrieval strategy.
- [Claude Code Integration](./docs/how-to/setup-claude-code.md): Give Claude Code long-term memory.
- [MCP Integration](./docs/how-to/using-mcp.md): Connecting Memex to Claude Desktop, Cursor, and other MCP clients.
- [Batch Ingestion](./docs/how-to/batch-ingestion.md): Importing your existing notes.
- [Vaults](./docs/how-to/organize-with-vaults.md): Isolating project knowledge.
- [Database Migrations](./docs/how-to/database-migrations.md): Managing schema migrations with `memex db`.

### Reference
- [CLI Commands](./docs/reference/cli-commands.md)
- [Configuration Schema](./docs/reference/configuration.md)
- [MCP Tools](./docs/reference/mcp-tools.md)
- [REST API](./docs/reference/rest-api.md)

### Management
- [Deleting and Archiving](./docs/how-to/delete-archival.md): Managing your knowledge base.

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue.

## 🏗️ Architecture

Memex is built as a monorepo:
- **`packages/core`**: The brain. Extraction, Retrieval (TEMPR), Reflection, services, FastAPI server.
- **`packages/cli`**: The interface. Typer CLI commands.
- **`packages/mcp`**: The bridge. FastMCP server for AI agent integration.
- **`packages/common`**: The foundation. Shared models, config, and exceptions.
- **`packages/dashboard`**: The view. React + Vite web UI for exploring your knowledge graph.
- **`packages/openclaw`**: The plugin. Memex memory integration for OpenClaw agents.

## License

[MIT](LICENSE.txt)
