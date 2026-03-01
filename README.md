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

> [!NOTE]
> Features like AI-generated answers, fact extraction, and reflection require an LLM API key. By default, Memex uses Gemini and needs `GEMINI_API_KEY` set in your environment. See [Configure Memex](./docs/how-to/configure-memex.md) for other model providers.

### Memory Search
Search across your knowledge base with TEMPR multi-strategy retrieval.

![Memory search showing results for Python memory management](assets/memex_cli_memory.gif)

### Memory Search with AI Answer
Get synthesized answers from your memories using `--answer`.

![Memory search with AI-generated answer](assets/memex_cli_memory_answer.gif)

### Note Search with Reasoning
Find relevant documents with LLM-powered relevance reasoning using `--reason`.

![Note search with reasoning annotations](assets/memex_cli_docs.gif)

### Entity Explorer
Browse and explore entities extracted from your knowledge base.

![Entity list and related entity exploration](assets/memex_cli_entities.gif)

### System Stats
Monitor your Memex instance at a glance.

![System statistics overview](assets/memex_cli_stats.gif)

### URL Ingestion
Capture web content directly into your knowledge base.

![Ingesting a URL into Memex](assets/memex_cli_ingest.gif)

### Dashboard
Explore your knowledge graph through the web UI.

![Dashboard overview with stats and recent activity](assets/memex_dashboard.gif)

### Entity Graph
Visualize entity relationships and co-occurrences.

![Interactive entity graph visualization](assets/memex_dashboard_entity_graph.gif)

### Lineage
Trace the provenance chain from notes to memories, observations, and mental models.

![Entity lineage showing provenance graph](assets/memex_dashboard_lineage.gif)

### Knowledge Flow
Monitor live ingestion activity across the knowledge pipeline.

![Knowledge flow pipeline with activity columns](assets/memex_dashboard_knowledge_flow.gif)

### Memory Search (Dashboard)
Search memories with multi-strategy retrieval and AI summaries.

![Dashboard memory search with results](assets/memex_dashboard_memory_search.gif)

## 📚 Documentation

Comprehensive guides and references are available in [`docs/`](./docs/index.md).

### Tutorials
- [Getting Started](./docs/tutorials/getting-started.md): Install, configure, ingest, and search.
- [Using the Dashboard](./docs/tutorials/using-the-dashboard.md): Explore the web UI.
- [AI Agent Memory](./docs/tutorials/ai-agent-memory.md): Build a Python agent with persistent memory.

### How-To Guides
- [Set Up Claude Code](./docs/how-to/setup-claude-code.md): Give Claude Code long-term memory with one command.
- [Configure Memex](./docs/how-to/configure-memex.md): YAML config, environment variables, model providers.
- [Using MCP](./docs/how-to/using-mcp.md): Connect to Claude Desktop, Cursor, and other MCP clients.
- [Organize with Vaults](./docs/how-to/organize-with-vaults.md): Isolate project knowledge.
- [Batch Ingestion](./docs/how-to/batch-ingestion.md): Import existing documents and notes.
- [Doc Search vs Memory Search](./docs/how-to/doc-search-vs-memory-search.md): Choose the right retrieval strategy.
- [Database Migrations](./docs/how-to/database-migrations.md): Manage schema with `memex db`.
- [OpenClaw Integration](./docs/how-to/openclaw-integration.md): Memex memory plugin for OpenClaw agents.
- [Delete and Archival](./docs/how-to/delete-archival.md): Manage data lifecycle.

### Reference
- [CLI Commands](./docs/reference/cli-commands.md)
- [REST API](./docs/reference/rest-api.md)
- [MCP Tools](./docs/reference/mcp-tools.md)
- [Configuration](./docs/reference/configuration.md)

### Explanation
- [Hindsight Framework](./docs/explanation/hindsight-framework.md): How Memex "thinks" and remembers.
- [Extraction Pipeline](./docs/explanation/extraction-pipeline.md): Fact extraction and entity resolution.
- [Retrieval Strategies](./docs/explanation/retrieval-strategies.md): TEMPR — five strategies fused via RRF.
- [Reflection and Mental Models](./docs/explanation/reflection-and-mental-models.md): Background synthesis of observations.
- [Dashboard Architecture](./docs/explanation/dashboard-architecture.md): React+Vite design and data flow.
- [OpenClaw Plugin](./docs/explanation/openclaw-plugin.md): Plugin lifecycle and circuit breaker.

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue.

## Releasing

Memex uses [semver](https://semver.org/) with unified versions across all Python packages. TypeScript packages are bumped alongside.

### How to determine the version bump

Look at the conventional commits since the last tag:

| Commit type | Bump | Example |
|---|---|---|
| `fix:` | **patch** (0.0.x) | `fix(core): handle null embeddings` |
| `feat:` | **minor** (0.x.0) | `feat(dashboard): add entity graph` |
| `feat!:` or `BREAKING CHANGE:` | **major** (x.0.0) | `feat!: change API response format` |

### Release workflow

```bash
# 1. Check what changed since last tag
git log --oneline $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~10)..HEAD

# 2. Bump all versions, commit, and tag
just release 0.1.0

# 3. Push (triggers the release workflow)
git push && git push --tags
```

The `release.yaml` GitHub Action automatically builds all artifacts (Python, dashboard, OpenClaw) and creates a GitHub Release with auto-generated release notes.

## 🏗️ Architecture

Memex is built as a monorepo:
- **`packages/core`**: The brain. Extraction, Retrieval (TEMPR), Reflection, services, FastAPI server.
- **`packages/cli`**: The interface. Typer CLI commands.
- **`packages/mcp`**: The bridge. FastMCP server for AI agent integration.
- **`packages/common`**: The foundation. Shared models, config, and exceptions.
- **`packages/dashboard`**: The view. React + Vite web UI for exploring your knowledge graph.
- **`packages/openclaw`**: The plugin. Memex memory integration for OpenClaw agents.

## Acknowledgements

Memex builds on ideas and code from these projects:

- **[Hindsight](https://github.com/vectorize-io/hindsight)** — the Hindsight retention engine formed the basis for Memex's memory system (extraction, retrieval, and reflection).
- **[PageIndex](https://github.com/VectifyAI/PageIndex)** — inspired the hierarchical page index used for structured note retrieval.

## License

[MIT](LICENSE.txt). See [NOTICES](NOTICES) for third-party attributions.
