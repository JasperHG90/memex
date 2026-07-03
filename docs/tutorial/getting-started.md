# Get started with Memex

This tutorial walks you through installing Memex, starting the server, and saving your first memory. By the end you will have a running instance, a vault holding one note, and a search query that returns it.

You will do everything from a terminal. Each step ends with output you can check against what you see on screen. If a step's output does not match, stop and fix it before moving on — later steps build on earlier ones.

Plan on 15 minutes from a clean machine.

## Prerequisites

- **Python 3.12 or newer.** Install from [python.org](https://www.python.org/downloads/). Python 3.13 also works.
- **Docker, running.** Install from [docs.docker.com](https://docs.docker.com/get-docker/). You will use it to run PostgreSQL.
- **uv version 0.10.0 or newer.** Install from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/). `uv` is Memex's only required package manager.
- **A terminal.** Bash and zsh both work.

You do not need an LLM API key to finish this tutorial. Memex's fact extraction step uses an LLM, but search returns useful results without it. The closing search query works on the note's raw text.

## Step 1: Install the Memex CLI

Install the `memex-cli` package, with the optional `[server]` extra so the same binary can run the API server:

```bash
uv tool install --refresh "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
```

The `--refresh` flag tells `uv` to ignore any cached build. The package installs as a tool, isolated from your project virtualenvs.

Now add a shell alias so you can call `memex` directly:

```bash
alias memex="uv tool run --from memex-cli memex"
```

Add the same line to your `~/.bashrc` or `~/.zshrc` to make it stick across terminal sessions.

Confirm the install:

```bash
memex --help
```

You should see a help screen listing command groups including `config`, `server`, `vault`, `note`, and `memory`. If the command is not found, the alias is missing — run the `alias` line again in the current shell.

## Step 2: Start PostgreSQL

Memex stores metadata, full-text search, and vectors in PostgreSQL with the pgvector extension. The simplest way to run one is the official pgvector image:

```bash
docker run -d \
  --name memex-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=memex \
  -p 5432:5432 \
  pgvector/pgvector:pg17
```

Check that the container is up:

```bash
docker ps --filter name=memex-postgres
```

You should see one row, with status `Up` and port `0.0.0.0:5432->5432/tcp`. If the row is missing, Docker is not running or the name collides with an existing container — `docker rm -f memex-postgres` clears the old one.

## Step 3: Initialize the configuration

`memex config init` writes a YAML config file with your database connection details. It prompts for each value:

```bash
memex config init
```

Enter the following at the prompts, matching the container from Step 2:

| Prompt   | Value      |
|----------|------------|
| Host     | localhost  |
| Port     | 5432       |
| Database | memex      |
| User     | postgres   |
| Password | postgres   |

The last prompt asks for the model name used for fact extraction. Press Enter to accept the default (`gemini/gemini-3-flash-preview`). You can change it later from the same config file, or override it per command.

You should see, on the final line:

```
Configuration successfully written to /home/<you>/.config/memex/config.yaml
```

The path is platform-specific — macOS shows `~/Library/Application Support/memex/config.yaml`, Linux shows `~/.config/memex/config.yaml`. Either is fine.

To check the resolved config any time, run `memex config show`.

## Step 4: Start the server

The CLI talks to a local HTTP server for everything except config commands. Start it in the background:

```bash
memex server start -d
```

The `-d` flag (short for `--daemon`) detaches the server so you keep your prompt. On first start, the server downloads embedding and reranking models — this can take a minute. Wait for the prompt to return.

Check that it is healthy:

```bash
memex server status
```

You should see two lines:

```
Server is running. PID: <pid>
Metrics endpoint reachable: http://localhost:8000/api/v1/metrics
Health check passed.
```

If you see `Server is NOT running`, check the log at `~/.local/state/memex/memex.log` (or run `memex server start` without `-d` to see startup errors directly).

## Step 5: Create a vault

A vault is a named collection. Notes, memories, and entities all live inside one. You can run many vaults side by side — for example, one for work and one for research — and search across them on demand.

Create one called `my-vault`:

```bash
memex vault create my-vault --description "My first vault"
```

You should see:

```
Creating vault: my-vault
Vault created successfully! ID: <uuid>
```

The UUID is the vault's permanent identifier. The name is a human alias — both work in later commands.

## Step 6: Add your first note

Add a short note to `my-vault`:

```bash
memex note add \
  --vault my-vault \
  --title "First note" \
  "Memex stores notes as Markdown and extracts memory units from them. \
Memory units are the atomic facts that retrieval and reflection work over."
```

The `--vault` (`-v`) flag picks the vault. The positional argument is the note's body. The CLI uploads the text and the server returns a note ID:

```
Adding Note
Note added successfully! UUID: <uuid>
```

If extraction finished before the command returned, you also see an `Extracted N memory units.` line. On a machine with an LLM API key set, the server extracts memory units in the background, so that line often appears on a later run rather than this one. Without a key, the note is still searchable by its raw text — fact extraction simply does not run, and no extraction line appears.

You can confirm the note is there:

```bash
memex note list --vault my-vault
```

You should see one row with the title `First note` and the ID returned above.

## Step 7: Search for it

Run a search across `my-vault`:

```bash
memex memory search "memory units" --vault my-vault
```

You should see output beginning with the query echo and at least one numbered result:

```
Searching: memory units
1. <id>
   Memex stores notes as Markdown and extracts memory units from them...
```

The result includes a snippet of the text you ingested and the unit ID. If results are empty, give the server a few more seconds — extraction runs asynchronously — and re-run the query.

You can also limit the strategies. For example, to search by keyword only:

```bash
memex memory search "memory units" --vault my-vault --no-semantic --no-graph --no-temporal --no-mental-model
```

This skips vector search and the entity and temporal strategies. The CLI prints which strategies are active above the results.

## What you built

You now have a Memex instance running on your machine:

- PostgreSQL with pgvector, listening on port 5432.
- The Memex server, listening on port 8000.
- One vault, `my-vault`, with one note inside it.
- A working search that finds the note by content.

Every command you ran in this tutorial works the same way against a remote Memex server — the CLI is a thin HTTP client. The same vault and the same search queries are also available over MCP, so an LLM agent can read and write the vault you just created.

## Next steps

You can take Memex in three directions from here.

**Add more content.** Use `memex note add --file <path>` to ingest a single file, or pass a directory to walk it. `memex note add --url <url>` scrapes a web page and ingests the cleaned text. Run `memex note add --help` for the full set.

**Set up an LLM provider.** With `GEMINI_API_KEY` (or another supported provider's key) in your environment, the server extracts memory units, resolves entities, and runs background reflection. Without it, search still works on raw text but the richer features stay dormant.

**Connect an agent.** The MCP server in `packages/mcp` exposes 35 tools backed by the same API. Claude Desktop, Cursor, and the Claude Code plugin all consume it. See the integration how-to below.

## See also

- [How-to: Configure Memex](../how-to/configuring-server/default-model.md) — change models, storage backends, and search defaults
- [Reference: CLI commands](../reference/cli.md) — every command group, every flag, every default
- [Explanation: How Memex retrieves memory](../explanation/retrieval.md) — the five strategies behind `memex memory search`

---

> Something not working? Run `memex report-bug` to open a pre-filled GitHub issue with your system info attached.
