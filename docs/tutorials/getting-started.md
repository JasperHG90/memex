# Getting Started with Memex

Memex is your personal knowledge vault, designed to help you capture, organize, and retrieve information using advanced AI techniques.

## Prerequisites

- Python 3.12+
- `uv` package manager (recommended) or `pip`
- Docker (optional, for running PostgreSQL/pgvector locally)

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/memex.git
cd memex

# Install dependencies
uv sync
```

## Initialization

Initialize your Memex configuration. This will create root config file and set up your local storage.

```bash
memex config init
```

By default, Memex uses:
- **FileStore**: Local filesystem (`~/.local/share/memex/files`)
- **MetaStore**: PostgreSQL (requires a running instance)
- **Model**: Gemini Flash (configurable)

You can override any root config setting in a scoped directory by creating a `.memex.yaml` file in that directory. That can be useful in e.g. project files where you want to store information in a project-related vault.

> [!NOTE]
> Memex uses `platformdirs` to resolve the appropriate cache and application directories to keep state, store logs, and download required files. Use `memex config show` to view the configuration. This will take into account any `.memex.yaml` files or CLI overrides passed to `--set`, for example: `memex --set "cli.server_url=localhost:8009" config show`

## Running the Server

Start the Memex API server to enable ingestion, search, and MCP integration.

**Note:** All subsequent commands require the server to be running.

```bash
memex server start -d
```

To view the server status, execute

```bash
memex server status
```

To stop the server, execute:

```bash
memex server stop
```

## Ingesting Content

> [!NOTE]
> In the examples below, we are not specifying a vault, which means that we store the notes in the **global** vault. For organizing notes with vaults, see ![Organizing With Vaults](../how-to/organize-with-vaults.md).

### 1. Ingest a URL

Capture a webpage, summarize it, and store it as a note.

```bash
memex memory add --url "https://example.com/article"
```

### 2. Ingest a Local File

Ingest a PDF, Markdown, or text file.

```bash
memex memory add --file ./docs/whitepaper.pdf
```

### 3. Batch Ingest a Directory

Recursively ingest a folder of documents.

```bash
memex memory add --file ./my-obsidian-vault/
```

### 4. Quick Note

You can also add a quick note directly from the CLI.

```bash
memex memory add "Project Idea: Build a CLI for Memex using Typer."
```

## Searching

Retrieve information using natural language.

```bash
memex memory search "What was that article about Memex architecture?"
```

## Next Steps

- [Using the Dashboard](using-the-dashboard.md)
- [Configure Memex](../how-to/configure-memex.md)
- [Organize with Vaults](../how-to/organize-with-vaults.md)
