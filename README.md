<p align="center">
  <img src="assets/memex-logo-spacy.png" width="160" alt="Memex Logo" />
</p>

<h1 align="center">Memex</h1>
<h3 align="center">Long-Term Memory for LLMs</h3>

<p align="center">
  Persistent, evolving knowledge for AI agents. Extracts facts, builds mental models, and retrieves with five-strategy fusion.<br/>
  <strong>Ingest anything. Remember everything. Retrieve what matters.</strong>
</p>

<p align="center">
  <a href="./docs/index.md">Documentation</a> &bull;
  <a href="./docs/tutorials/getting-started.md">Quick Start</a> &bull;
  <a href="#claude-code-plugin">Claude Code Plugin</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/language-Python-blue?style=flat-square" alt="Python" />
  <img src="https://img.shields.io/badge/python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square" alt="Apache 2.0" />
  <img src="https://img.shields.io/badge/version-v0.0.39a-green?style=flat-square" alt="v0.0.39a" />
  <img src="https://img.shields.io/badge/tests-2,633%20passing-brightgreen?style=flat-square" alt="Tests" />
</p>

## Requirements

1. Python 3.12+ (3.13 tested in CI)
2. [uv](https://docs.astral.sh/uv/) >= 0.10.0
3. PostgreSQL with pgvector

## 🚀 Quick Start

> [!NOTE]
> Features like AI-generated answers, fact extraction, and reflection require an LLM API key. By default, Memex uses Gemini and needs `GEMINI_API_KEY` set in your environment. See [Configure Memex](./docs/how-to/configure-memex.md) for other model providers.

### 1. Set up postgres

Download e.g. the [Postgres app](https://postgresapp.com/), or use docker for just the database: `docker compose up -d postgres` (see `docker-compose.yaml` in this repository).

### 2. Install
Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (>= 0.10.0).

```bash
uv tool install --refresh "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
```

It's easiest to just alias the `uv tool` command: `alias memex="uv tool run --from memex-cli memex"`

### 3. Initialize
Sets up your local storage and configuration.

```bash
memex config init
```

### 4. Start the Server
Memex requires a running API server for all operations.

```bash
# In a separate terminal
memex server start -d
```

### 5. Ingest
Feed it knowledge.

```bash
# Isolate notes with vaults
memex vault create notes --description "Notes about things"

# Inline note
memex note add -v notes "Memex provides long-term memory that evolves."

# Capture a webpage
# Goes to the 'global' vault
memex note add --url "https://docs.python.org/3/tutorial/"

# Point it to local files
# Supports: MD, PDF, docx, xlsx, outlook, pptx
memex note add --file /path/to/file.md --vault notes
```

### 6. Search
Ask questions.

```bash
memex memory search "How does Python handle memory management?"
```

## See it in action

### Claude Code Plugin

Give Claude Code persistent memory across all projects — no per-project setup needed.

```bash
# Add the Memex marketplace
claude plugin marketplace add JasperHG90/memex

# Install the plugin
claude plugin install memex@memex
```

Or from inside Claude Code: `/plugin marketplace add JasperHG90/memex` then `/plugin install memex@memex`.

The plugin provides `/remember` and `/recall` slash commands, session lifecycle hooks, behavioral instructions, and the Memex MCP server. See [packages/claude-code-plugin](./packages/claude-code-plugin/) for details.

#### Updating the claude code plugin

To update the claude code plugin, first execute `claude plugin marketplace update`, then `claude plugin update memex@memex` to update the claude code plugin.

#### Overriding defaults

- By default, the claude code plugin uses the MCP server from tag `latest`. To override this, you can specify a project-level memex MCP server in your project's `.mcp.json`.
- To override individual memex settings (e.g. MEMEX_BASE_URL), add these to './claude/settings.json', e.g.

```json
{
  "env": {
    "MEMEX_SERVER_URL": "http://host.docker.internal:8000"
  }
}
```

![Claude Code using Memex for long-term memory](assets/memex_claude_code.gif)

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

## Features

### Ingest anything

Feed Memex from any source — plain text, Markdown, PDFs, Word docs, PowerPoint, Excel, Outlook emails, web pages, or entire directories. File conversion is handled automatically via [MarkItDown](https://github.com/microsoft/markitdown) and [PyMuPDF](https://pymupdf.readthedocs.io/). Background and batch ingestion modes let you import large document collections without blocking. Pluggable note templates (built-in, global, and project-local `.toml` files) provide consistent structure for different note types.

```bash
memex note add "Quick inline note"
memex note add --file ./research-papers/        # directory of PDFs
memex note add --url https://example.com/article
memex note add --file report.md --asset diagram.png --background
```

#### Firefox extension

A [Firefox extension](./packages/firefox-extension/) for one-click capture of articles, PDFs, and web pages directly into your Memex vaults. Content is extracted client-side via Mozilla Readability and converted to Markdown — bypassing bot detection and paywalled content that server-side scraping can't reach. API keys are encrypted at rest with AES-GCM.

![firefox](./assets/firefox-extension.png)

### Five-strategy retrieval (TEMPR)

Every search runs five independent retrieval strategies in parallel and fuses them with Reciprocal Rank Fusion — no single strategy has to be "right":

| Strategy | What it finds |
|:---------|:--------------|
| **Semantic** | Conceptually similar facts via pgvector cosine distance |
| **Keyword** | Exact term matches via PostgreSQL full-text search |
| **Graph** | Entity-linked facts via NER, phonetic matching, and co-occurrence traversal |
| **Temporal** | Recent facts via exponential time-decay scoring |
| **Mental Model** | High-level synthesized insights from the reflection engine |

Post-fusion, MMR diversity filtering prunes near-duplicates using a hybrid cosine + entity Jaccard kernel. Optional `after`/`before` date bounds and `tags` filters let you scope any search.

### Hierarchical page index

Long documents are split into a structured table of contents with section-level summaries, token estimates, and unique node IDs. Read a 50-page PDF section by section instead of dumping the entire document into context. The page index powers skeleton-tree reasoning (`--reason`) and targeted answer synthesis (`--summarize`).

### Incremental extraction

When you update a note (via `note_key`), Memex diffs the content against the previous version and only re-extracts changed blocks. Unchanged facts, entities, and embeddings are preserved — saving LLM calls and keeping ingestion fast for living documents.

### Contradiction detection

New facts are automatically triaged for corrections and updates. When a newer note contradicts or supersedes an older one, confidence scores are adjusted and supersession links are recorded. Retrieval naturally favors the most current information without manual cleanup.

### Reflection and mental models

A background reflection loop periodically reviews entities with new evidence, synthesizes observations, and builds versioned mental models. Over time, Memex evolves from a collection of raw facts into structured understanding — "The team consistently prioritizes performance over feature velocity" emerges from dozens of individual meeting notes.

### Vaults

Isolate knowledge by project, team, or topic. Each vault is a self-contained scope for notes, memories, entities, and mental models. Policy-based access control (reader/writer/admin) with vault-scoped API keys lets you grant fine-grained permissions. Use `read_vault_ids` for cross-vault read access without write permissions.

### Cloud-native storage

The file store uses [fsspec](https://filesystem-spec.readthedocs.io/) for backend-agnostic storage. Swap between local disk, Amazon S3, and Google Cloud Storage with a config change:

```yaml
server:
  file_store:
    type: s3            # or 'gcs', 'local'
    root: my-bucket/memex
```

### AI agent integration

First-class support for Claude Code, Claude Desktop, Cursor, and any MCP-compatible client. Install the [Claude Code plugin](#claude-code-plugin) for one-step setup across all projects, or use `memex setup claude-code` for per-project configuration. 31 MCP tools cover the full API surface. A slim Docker image (`docker/mcp/Dockerfile`) enables containerized MCP deployment with HTTP transport.

### REST API and webhooks

A full FastAPI server with NDJSON streaming, OpenAPI docs, policy-based auth (reader/writer/admin) with vault-scoped API keys, rate limiting, and outgoing webhook subscriptions for event-driven integrations (`ingestion.completed`, `reflection.completed`).

### Tight integration with MCP

The MCP server allows any agent to retrieve information from and add notes to Memex.

### Note templates

Pluggable note templates provide consistent structure for different note types. Templates are `.toml` files discovered across three layers — built-in, global (`{filestore_root}/templates/`), and project-local (`.memex/templates/`) — with later layers overriding on slug collision. Built-in templates include `technical_brief`, `general_note`, `architectural_decision_record`, `request_for_comments`, and `quick_note`. Manage templates via `memex note template` CLI commands or the `memex_list_templates` / `memex_register_template` MCP tools.

### Folder sync

Sync a folder of Markdown notes (and PDFs, Word docs, Excel, PowerPoint, Outlook emails, and more) to Memex with `memex note sync`. Incremental sync tracks state locally — only changed files are re-processed. Deleted files are archived by default (preserving data, excluding from retrieval). Background batch mode, continuous watch mode (event-driven or polling), and a layered TOML config (`note-sync.toml`) make it easy to keep an Obsidian vault or any notes folder in sync.

```bash
memex note sync init ~/notes          # create default config
memex note sync run ~/notes           # sync changed files
memex note sync watch ~/notes         # continuous sync
```

## 📚 Documentation

Comprehensive guides and references are available in [`docs/`](./docs/index.md).

### Tutorials
- [Getting Started](./docs/tutorials/getting-started.md): Install, configure, ingest, and search.
- [AI Agent Memory](./docs/tutorials/ai-agent-memory.md): Build a Python agent with persistent memory.

### How-To Guides
- [Set Up Claude Code](./docs/how-to/setup-claude-code.md): Give Claude Code long-term memory via the plugin or setup command.
- [Configure Memex](./docs/how-to/configure-memex.md): YAML config, environment variables, model providers.
- [Using MCP](./docs/how-to/using-mcp.md): Connect to Claude Desktop, Cursor, and other MCP clients.
- [Organize with Vaults](./docs/how-to/organize-with-vaults.md): Isolate project knowledge.
- [Batch Ingestion](./docs/how-to/batch-ingestion.md): Import existing documents and notes.
- [Doc Search vs Memory Search](./docs/how-to/doc-search-vs-memory-search.md): Choose the right retrieval strategy.
- [Database Migrations](./docs/how-to/database-migrations.md): Manage schema with `memex db`.
- [Delete and Archival](./docs/how-to/delete-archival.md): Manage data lifecycle.
- [Note Templates](./docs/how-to/note-templates.md): Create and use note templates.
- [Sync Notes](./docs/how-to/sync-notes.md): Sync a folder of notes to Memex.
- [Firefox Extension](./docs/how-to/firefox-extension.md): Capture web content from Firefox.

### Reference
- [CLI Commands](./docs/reference/cli-commands.md)
- [REST API](./docs/reference/rest-api.md)
- [MCP Tools](./docs/reference/mcp-tools.md)
- [MemexAPI](./docs/reference/memexapi-reference.md): Python API class — 60+ public methods.
- [Configuration](./docs/reference/configuration.md)
- [Evaluation Report](./docs/reference/evaluation-report.md): LoCoMo benchmark results, retrieval efficiency, and per-question analysis.

### Explanation
- [Hindsight Framework](./docs/explanation/hindsight-framework.md): How Memex "thinks" and remembers.
- [Extraction Pipeline](./docs/explanation/extraction-pipeline.md): Fact extraction and entity resolution.
- [Retrieval Strategies](./docs/explanation/retrieval-strategies.md): TEMPR — five strategies fused via RRF.
- [Reflection and Mental Models](./docs/explanation/reflection-and-mental-models.md): Background synthesis of observations.
> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue.

## Releasing

Memex uses [semver](https://semver.org/) with unified versions across all Python packages. TypeScript packages are bumped alongside.

### How to determine the version bump

Look at the conventional commits since the last tag:

| Commit type | Bump | Example |
|---|---|---|
| `fix:` | **patch** (0.0.x) | `fix(core): handle null embeddings` |
| `feat:` | **minor** (0.x.0) | `feat(core): add entity graph` |
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

The `release.yaml` GitHub Action automatically builds all artifacts and creates a GitHub Release with auto-generated release notes.

## Evaluation

Memex is benchmarked against [LoCoMo](https://arxiv.org/abs/2402.17753), an academic benchmark for long-term conversational memory. The benchmark tests fact recall, temporal reasoning, multi-hop inference, and adversarial robustness across 19-session dialogues. Memex is evaluated on a subset of 47 QA pairs from the first conversation only (out of 50 conversations in the full dataset).

### LoCoMo results

| Category | Count | Mean Score |
|---|---|---|
| Single-Hop | 9 | 0.944 |
| Multi-Hop | 9 | 1.000 |
| Open Domain | 3 | 1.000 |
| Temporal | 15 | 1.000 |
| **Non-adversarial** | **36** | **0.986** |
| Adversarial (unweighted) | 11 | 0.773 |

Answering model: Claude Opus 4 via Claude Code. Judging model: Gemini 3 Flash. Scores are on a 0-1 graded scale after manual review of judge assessments. 3 image-dependent questions excluded. Adversarial scores reported separately — see [full evaluation report](./docs/reference/evaluation-report.md) for methodology, retrieval efficiency analysis, and per-question details. See [`packages/eval`](./packages/eval/README.md) for the evaluation framework and reproduction instructions.

### Retrieval efficiency

Memex retrieval adds minimal overhead to agent workflows. Across the 47-question benchmark:

| Metric | Value |
|---|---|
| Retrieval tokens per question (median) | **4,609** |
| Retrieval tokens per question (mean) | 7,592 |
| Retrieval as % of total tokens | **4.5%** |

95% of token usage is agent overhead (system prompt, tool definitions, conversation history). The Memex MCP tools themselves return compact results — a typical question needs just one `memory_search` call (~2.4K tokens) or a two-stage `memory_search` + `note_search` (~3.4K tokens). Complex multi-hop questions that drill into specific note sections via the page index cost ~6K retrieval tokens.

## 🏗️ Architecture

Memex is built as a monorepo:
- **`packages/core`**: The brain. Extraction, Retrieval (TEMPR), Reflection, services, FastAPI server.
- **`packages/cli`**: The interface. Typer CLI commands, including `memex note sync` for folder-based note synchronization.
- **`packages/mcp`**: The bridge. FastMCP server for AI agent integration.
- **`packages/common`**: The foundation. Shared models, config, and exceptions.
- **`packages/eval`**: The benchmark. LoCoMo evaluation framework and retrieval analysis.
- **`packages/claude-code-plugin`**: The plugin. Claude Code plugin for cross-project memory integration.
- **`packages/firefox-extension`**: The capture. Firefox extension for web content ingestion.

## Acknowledgements

Memex builds on ideas and code from these projects:

- **[Hindsight](https://github.com/vectorize-io/hindsight)** — the Hindsight retention engine formed the basis for Memex's memory system (extraction, retrieval, and reflection).
- **[PageIndex](https://github.com/VectifyAI/PageIndex)** — inspired the hierarchical page index used for structured note retrieval.

## License

[Apache 2.0](LICENSE.txt). See [NOTICES](NOTICES) for third-party attributions.
