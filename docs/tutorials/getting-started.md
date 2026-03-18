# Tutorial: Set Up Memex and Store Your First Memory

In this tutorial, we will install Memex, start the server, create a vault, and ingest our first piece of knowledge. By the end, you will have a working Memex instance that can store and search memories.

## Prerequisites

* **Python 3.12+** installed ([python.org](https://www.python.org/downloads/)) — 3.13 also supported
* **Docker** installed and running ([docs.docker.com](https://docs.docker.com/get-docker/)) — needed for PostgreSQL with pgvector
* **uv** >= 0.10.0 package manager installed ([docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/))

## Step 1: Install Memex

First, we need to install the Memex CLI tool. We will use `uv tool install` to install it globally.

```bash
uv tool install --refresh "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
```

This installs the `memex` CLI along with the server package.

Next, let's create a shell alias so we can run `memex` directly:

```bash
alias memex="uv tool run --from memex-cli memex"
```

> [!TIP]
> Add the alias to your `~/.bashrc` or `~/.zshrc` to make it permanent.

Let's verify the installation works:

```bash
memex --help
```

We should see output showing the Memex CLI help with available commands like `config`, `server`, `memory`, `vault`, and others.

## Step 2: Start PostgreSQL with pgvector

Memex uses PostgreSQL with the pgvector extension for metadata storage and vector search. Let's start a PostgreSQL instance using Docker:

```bash
docker run -d \
  --name memex-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=memex \
  -p 5432:5432 \
  pgvector/pgvector:pg17
```

We can verify it is running:

```bash
docker ps --filter name=memex-postgres
```

We should see the container listed with status "Up".

## Step 3: Initialize the Configuration

Now let's set up the Memex configuration. This creates a config file with database connection details and default settings.

```bash
memex config init
```

The command will prompt us for PostgreSQL connection details. Enter the following values (matching the Docker container we started):

| Prompt   | Value      |
|----------|------------|
| Host     | localhost  |
| Port     | 5432       |
| Database | memex      |
| User     | postgres   |
| Password | postgres   |

It will also ask for a model name for fact extraction. Press Enter to accept the default (`gemini/gemini-3-flash-preview`).

We should see:

```
Configuration successfully written to /home/<user>/.config/memex/config.yaml
```

> [!TIP]
> To view the full configuration at any time, run `memex config show`.

## Step 4: Start the Memex Server

Memex requires a running API server for all operations. Let's start it in daemon (background) mode:

```bash
memex server start -d
```

We should see output confirming the server started. Let's verify it is healthy:

```bash
memex server status
```

We should see a message indicating the server is running and healthy.

## Step 5: Create a Vault

Vaults let us organize knowledge into separate collections. Let's create our first vault:

```bash
memex vault create notes --description "Notes about things"
```

We should see confirmation that the vault was created. Vaults keep different knowledge domains isolated — for example, we might have one vault for work notes and another for personal research.

## Step 6: Add a Memory

Now let's ingest our first piece of knowledge. We will add a quick inline note to the vault we just created:

```bash
memex note add -v notes "Memex provides long-term memory that evolves. It extracts structured facts from unstructured data and synthesizes mental models over time."
```

We should see a response confirming the note was ingested, including a note ID.

Memex processes the text in the background: it extracts facts, resolves entities, and generates embeddings for semantic search.

## Step 7: Search Your Memories

Let's query Memex to retrieve what we stored:

```bash
memex memory search "How does Memex handle memory?"
```

We should see results containing the facts extracted from our note, ranked by relevance. Memex uses five retrieval strategies (semantic, keyword, temporal, entity graph, and mental model) and fuses results using Reciprocal Rank Fusion.

## Conclusion

We have successfully installed Memex, configured the database, started the server, and stored and retrieved our first memory. We now have a working knowledge base that can grow over time.

## Next Steps

* [Using the Dashboard](using-the-dashboard.md) — explore your knowledge visually
* [Configure Memex](../how-to/configure-memex.md) — customize models, storage backends, and more
* [Set Up Claude Code Integration](../how-to/setup-claude-code.md) — give Claude Code long-term memory with one command
* [Organize with Vaults](../how-to/organize-with-vaults.md) — manage multiple knowledge domains
* [Batch Ingestion](../how-to/batch-ingestion.md) — import existing documents and notes
* [Using MCP](../how-to/using-mcp.md) — connect Memex to Claude Desktop, Cursor, and other MCP clients

> **Something not working?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info attached.
