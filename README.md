# Memex

Memex is a long-term memory system designed to give LLMs persistent, evolving knowledge. It captures unstructured data (notes, docs, chats), extracts structured facts, and synthesizes high-level mental models over time.

## 🚀 Quick Start

### 1. Install
Requires Python 3.12+ and `uv`.

```bash
# Clone and sync
uv tool install git+https://github.com/JasperHG90/memex.git[mcp,dashboard,server]
```

It's easiest to just alias the `uv tool` command: `alias memex="uv tool run --from memex-cli memex"`

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
memex memory add -v notes "Memex provides long-term mmemory that evolves."

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
uv run memex memory search "How does Python handle memory management?"
```

## 📚 Documentation

Comprehensive guides and references are available in [`docs/`](./docs).

### Basics
- [Getting Started](./docs/tutorials/getting-started.md)
- [Configuration](./docs/how-to/configure-memex.md)
- [Using the Dashboard](./docs/tutorials/using-the-dashboard.md)

### Key Features
- [Hindsight Framework](./docs/explanation/hindsight-framework.md): How Memex "thinks" and remembers.
- [Extraction Pipeline](./docs/explanation/extraction-pipeline.md): Understanding fact extraction.
- [Doc Search vs Memory Search](./docs/how-to/doc-search-vs-memory-search.md): Choosing the right retrieval strategy.
- [Batch Ingestion](./docs/how-to/batch-ingestion.md): Importing your existing notes.
- [MCP Integration](./docs/how-to/using-mcp.md): Connecting Memex to Claude/IDEs.
- [Vaults](./docs/how-to/organize-with-vaults.md): Isolating project knowledge.

### Reference
- [CLI Commands](./docs/reference/cli-commands.md)
- [Configuration Schema](./docs/reference/configuration.md)
- [MCP Tools](./docs/reference/mcp-tools.md)
- [REST API](./docs/reference/rest-api.md)

### Management
- [Deleting and Archiving](./docs/how-to/delete-archival.md): Managing your knowledge base.

## 🏗️ Architecture

Memex is built as a Python monorepo:
- **`packages/core`**: The brain. Extraction, Retrieval (TEMPR), Reflection, utilities, PageIndex.
- **`packages/cli`**: The interface. Management commands.
- **`packages/mcp`**: The bridge. Connects to AI agents.
- **`packages/dashboard`**: The view. Visual knowledge graph.

## License

[MIT](LICENSE.txt)
