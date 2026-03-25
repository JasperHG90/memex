# CLI Reference

The `memex` command-line interface for managing the Memex knowledge management system. Built with [Typer](https://typer.tiangolo.com/).

## Global Options

These options apply to all commands and must be specified before the subcommand.

| Option | Short | Description |
|--------|-------|-------------|
| `--config PATH` | `-c` | Path to the configuration file. Defaults to `~/.config/memex/config.yaml`, then searches CWD for `memex_core.yaml`, `.memex.yaml`, or `memex_core.config.yaml`. Can also be set via `MEMEX_CONFIG_PATH` env var. |
| `--set KEY=VALUE` | `-s` | Override config values using dot notation. Repeatable. Example: `--set server.meta_store.instance.host=localhost`. |
| `--vault NAME` | `-v` | Override the active vault for this command. |
| `--debug` | `-d` | Enable debug logging (to console and log file). |
| `--help` | `-h` | Show help message and exit. |

### Configuration Resolution Order

1. CLI `--set` overrides (highest priority)
2. Environment variables (`MEMEX_*`, nested with `__`)
3. Local config (`memex_core.yaml`, `.memex.yaml`, or `memex_core.config.yaml` in CWD or parents)
4. Global config (`~/.config/memex/config.yaml`)
5. Defaults

---

## `memory`

Ingest and search memories.

> **Note:** `memory add` is a legacy alias for `note add`. Both commands accept the same options and produce identical results. Prefer `note add` for new workflows.

### `memory add`

```
memex memory add [CONTENT] [OPTIONS]
```

Add a new memory to Memex. Accepts text content directly, a file/directory path, or a URL. Use `--asset` to attach auxiliary files (images, PDFs) to a note.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `CONTENT` | No | Text content to add. Required if `--file` and `--url` are not provided. |

#### Options

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--file PATH` | `-f` | Path | Path to a file or directory to ingest. Directories are scanned recursively. |
| `--url URL` | `-u` | str | URL to scrape and ingest. |
| `--asset PATH` | `-a` | Path | Path to an asset file (image, PDF) to attach. Repeatable for multiple assets. |
| `--vault NAME` | `-v` | str | Target vault for writing (overrides active vault). |
| `--key KEY` | `-k` | str | Unique stable key for the note (enables idempotent updates). |
| `--background` | `-b` | bool | Queue ingestion as a background job instead of waiting for completion. |

#### Examples

```bash
# Add text content
memex memory add "The project uses PostgreSQL with pgvector for storage."

# Ingest a file
memex memory add --file ./notes/meeting.md

# Ingest a directory recursively
memex memory add --file ./research-papers/

# Scrape and ingest a URL
memex memory add --url https://example.com/article

# Add with attached assets
memex memory add --file ./report.md --asset ./diagram.png --asset ./data.csv

# Background ingestion
memex memory add --file ./large-dataset/ --background
```

> [!WARNING]
> `--asset` cannot be used with a directory `--file`. Point `--file` to a single file when using `--asset`.

---

### `note add`

```
memex note add [CONTENT] [OPTIONS]
```

Add a new note to Memex. Accepts text content directly, a file/directory path, or a URL.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `CONTENT` | No | Text content to add. Required if `--file` and `--url` are not provided. |

#### Options

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--file PATH` | `-f` | Path | Path to a file or directory to ingest. Directories are scanned recursively. |
| `--url URL` | `-u` | str | URL to scrape and ingest. |
| `--asset PATH` | `-a` | Path | Path to an asset file (image, PDF) to attach. Repeatable for multiple assets. |
| `--vault NAME` | `-v` | str | Target vault for writing (overrides active vault). |
| `--key KEY` | `-k` | str | Unique stable key for the note (enables idempotent updates). |
| `--background` | `-b` | bool | Queue ingestion as a background job instead of waiting for completion. |

#### Examples

```bash
# Add text content
memex note add "The project uses PostgreSQL with pgvector for storage."

# Ingest a file
memex note add --file ./notes/meeting.md

# Ingest a directory recursively
memex note add --file ./research-papers/

# Scrape and ingest a URL
memex note add --url https://example.com/article

# Add a note with attached assets
memex note add --file ./report.md --asset ./diagram.png --asset ./data.csv

# Add with a stable key (for updates)
memex note add --file ./daily-log.md --key daily-log-2025-01-15

# Background ingestion
memex note add --file ./large-dataset/ --background
```

> [!WARNING]
> `--asset` cannot be used with a directory `--file`. Point `--file` to a single file when using `--asset`.

---

### `memory search`

```
memex memory search QUERY [OPTIONS]
```

Search the knowledge base using TEMPR retrieval strategies.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `QUERY` | Yes | Search query string. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--vault` | `-v` | str (list) | - | Filter by vault(s). Repeatable. |
| `--limit` | | int | `5` | Maximum number of results to return. |
| `--token-budget` | `-t` | int | - | Token budget for retrieval context. |
| `--answer` | `-a` | bool | `False` | Generate an AI-synthesized answer from results. |
| `--json` | | bool | `False` | Output results as JSON. |
| `--minimal` | | bool | `False` | Output memory unit IDs only (one per line). |
| `--no-semantic` | | bool | `False` | Exclude semantic (vector) strategy. |
| `--no-keyword` | | bool | `False` | Exclude keyword (BM25) strategy. |
| `--no-graph` | | bool | `False` | Exclude graph (entity) strategy. |
| `--no-temporal` | | bool | `False` | Exclude temporal strategy. |
| `--no-mental-model` | | bool | `False` | Exclude mental model strategy. |

#### Examples

```bash
# Basic search
memex memory search "PostgreSQL connection pooling"

# Search with AI answer generation
memex memory search "How does reflection work?" --answer

# Search specific vaults
memex memory search "auth patterns" --vault project-a --vault project-b

# JSON output for scripting
memex memory search "database schema" --json --limit 10

# Search with only keyword and semantic strategies
memex memory search "error handling" --no-graph --no-temporal --no-mental-model
```

---

### `memory delete`

```
memex memory delete UNIT_ID [OPTIONS]
```

Delete a memory unit and all associated data (entity links, memory links).

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `UNIT_ID` | Yes | UUID of the memory unit to delete. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

---

### `memory reflect`

```
memex memory reflect [ENTITY_ID] [OPTIONS]
```

Manually trigger a reflection cycle. Reflection synthesizes observations about entities into mental models.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `ENTITY_ID` | No | UUID of a specific entity to reflect on. If omitted, processes items from the reflection queue or top entities. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit` | int | `5` | Number of entities to process when no entity ID is provided. |
| `--batch-size` | int | `10` | Number of entities to process per batch. |

#### Examples

```bash
# Reflect on a specific entity
memex memory reflect 550e8400-e29b-41d4-a716-446655440000

# Process top 10 entities from the reflection queue
memex memory reflect --limit 10
```

---

### `memory lineage`

```
memex memory lineage ENTITY_TYPE ENTITY_ID [OPTIONS]
```

Visualize the provenance lineage of an entity as a tree.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `ENTITY_TYPE` | Yes | Type of entity. One of: `mental_model`, `observation`, `memory_unit`, `note`. |
| `ENTITY_ID` | Yes | UUID of the entity. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--direction` | `-d` | str | `upstream` | Traverse direction: `upstream` or `downstream`. |
| `--depth` | | int | `3` | Maximum recursion depth. |
| `--limit` | | int | `5` | Maximum children per node. |
| `--json` | | bool | `False` | Output as JSON instead of a tree visualization. |

#### Examples

```bash
# View upstream lineage of a memory unit
memex memory lineage memory_unit 550e8400-e29b-41d4-a716-446655440000

# View downstream lineage of a mental model
memex memory lineage mental_model 550e8400-e29b-41d4-a716-446655440000 --direction downstream

# JSON output with deeper traversal
memex memory lineage note 550e8400-e29b-41d4-a716-446655440000 --depth 5 --json
```

---

## `note`

Manage and view source notes.

### `note list`

```
memex note list [OPTIONS]
```

List all notes in the current vault.

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit` | int | `50` | Maximum number of notes to return. |
| `--offset` | int | `0` | Pagination offset. |
| `--json` | bool | `False` | Output as JSON. |
| `--minimal` | bool | `False` | Output one note ID per line. |

---

### `note recent`

```
memex note recent [OPTIONS]
```

Show most recently created notes.

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit` | int | `10` | Maximum number of notes to return. |
| `--json` | bool | `False` | Output as JSON. |
| `--minimal` | bool | `False` | Output one note ID per line. |

---

### `note search`

```
memex note search QUERY [OPTIONS]
```

Search for notes using multi-channel fusion (Reciprocal Rank Fusion).

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `QUERY` | Yes | Search query string. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--limit` | `-l` | int | `5` | Maximum number of notes to return. |
| `--expand` | | bool | `False` | Enable LLM-powered query expansion. |
| `--blend` | | bool | `False` | Enable position-aware blending (instead of default RRF). |
| `--vault` | `-v` | str (list) | - | Vault(s) to search. Repeatable. |
| `--reason` | | bool | `False` | Run skeleton-tree identification; shows relevant sections with reasoning. |
| `--summarize` | | bool | `False` | Synthesize a full answer from matched sections (implies `--reason`). |
| `--json` | | bool | `False` | Output as JSON. |
| `--minimal` | | bool | `False` | Output note IDs only. |
| `--no-semantic` | | bool | `False` | Exclude semantic (vector) strategy. |
| `--no-keyword` | | bool | `False` | Exclude keyword (BM25) strategy. |
| `--no-graph` | | bool | `False` | Exclude graph (entity) strategy. |
| `--no-temporal` | | bool | `False` | Exclude temporal strategy. |

#### Examples

```bash
# Basic note search
memex note search "database migration"

# Search with reasoning about relevant sections
memex note search "authentication flow" --reason

# Get a synthesized answer
memex note search "How to configure webhooks?" --summarize

# Search with query expansion
memex note search "connection errors" --expand --limit 10
```

---

### `note view`

```
memex note view NOTE_ID [OPTIONS]
```

View the full content and metadata of a note.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `NOTE_ID` | Yes | UUID of the note. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `note page-index`

```
memex note page-index NOTE_ID [OPTIONS]
```

View the page index (hierarchical table of contents) of a note. Only available for notes ingested with the page-index strategy.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `NOTE_ID` | Yes | UUID of the note. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `note node`

```
memex note node NODE_ID [OPTIONS]
```

View a specific page-index node (section) by its ID. Node IDs are found in the output of `note page-index` or `note search --reason`.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `NODE_ID` | Yes | UUID of the node. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `note delete`

```
memex note delete NOTE_ID [OPTIONS]
```

Delete a note and all associated data (memory units, chunks, links, assets).

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `NOTE_ID` | Yes | UUID of the note to delete. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

---

## `entity`

Explore and manage extracted entities (people, organizations, concepts).

### `entity list`

```
memex entity list [OPTIONS]
```

List entities ranked by mention count, or search by name.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--limit` | `-l` | int | `50` | Maximum number of entities to show. |
| `--query` | `-q` | str | - | Filter entities by name (search query). |
| `--json` | | bool | `False` | Output as JSON. |

#### Examples

```bash
# List top 50 entities by mention count
memex entity list

# Search for entities by name
memex entity list --query "PostgreSQL"
```

---

### `entity view`

```
memex entity view IDENTIFIER [OPTIONS]
```

View details of a specific entity. Accepts either a name or UUID.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the entity. If the name is ambiguous, displays matching candidates. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `entity mentions`

```
memex entity mentions IDENTIFIER [OPTIONS]
```

Show memories and notes that mention this entity.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the entity. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit` | int | `20` | Maximum number of mentions to show. |
| `--json` | bool | `False` | Output as JSON. |

---

### `entity related`

```
memex entity related IDENTIFIER [OPTIONS]
```

Show entities that frequently co-occur with the given entity, ranked by co-occurrence count.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the entity. |

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `entity delete`

```
memex entity delete IDENTIFIER [OPTIONS]
```

Delete an entity and all associated data (mental models, aliases, links, co-occurrences).

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the entity to delete. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

---

### `entity delete-mental-model`

```
memex entity delete-mental-model IDENTIFIER [OPTIONS]
```

Delete the mental model for an entity in a specific vault. Does not delete the entity itself.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the entity. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--vault` | `-v` | str | Active vault | Vault UUID to target. Defaults to the active vault. |
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

---

## `kv`

Key-value fact store (lightweight structured memory).

### `kv write`

```
memex kv write VALUE [OPTIONS]
```

Write a fact to the KV store. Key is required (use MCP tool for auto-generation).

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `VALUE` | Yes | The fact/value to store. |

#### Options

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--key KEY` | `-k` | str | Namespaced key, e.g. `"tool:python:pkg_mgr"`. **Required.** |
| `--vault NAME` | `-v` | str | Target vault name or UUID. |

#### Examples

```bash
# Store a preference
memex kv write "always use uv, never pip" --key "tool:python:pkg_mgr"

# Store a vault-scoped fact
memex kv write "Staff Engineer" --key "user:role" --vault my-project
```

---

### `kv get`

```
memex kv get KEY [OPTIONS]
```

Get a fact by exact key.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `KEY` | Yes | Key to look up. |

#### Options

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--vault NAME` | `-v` | str | Vault name or UUID. |

#### Examples

```bash
memex kv get "tool:python:pkg_mgr"
memex kv get "user:role" --vault my-project
```

---

### `kv search`

```
memex kv search QUERY [OPTIONS]
```

Fuzzy search facts by semantic similarity.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `QUERY` | Yes | Search query. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--limit` | `-l` | int | `5` | Maximum results to return. |
| `--vault` | `-v` | str | - | Vault name or UUID. |
| `--json` | | bool | `False` | Output as JSON. |

#### Examples

```bash
memex kv search "python package manager"
memex kv search "deployment" --limit 10 --json
```

---

### `kv list`

```
memex kv list [OPTIONS]
```

List all facts in the KV store.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--vault` | `-v` | str | - | Vault name or UUID. |
| `--json` | | bool | `False` | Output as JSON. |

#### Examples

```bash
memex kv list
memex kv list --vault my-project --json
```

---

### `kv delete`

```
memex kv delete KEY [OPTIONS]
```

Delete a fact by key.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `KEY` | Yes | Key to delete. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--vault` | `-v` | str | - | Vault name or UUID. |
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

#### Examples

```bash
memex kv delete "tool:python:pkg_mgr"
memex kv delete "user:role" --vault my-project --force
```

---

## `vault`

Manage Memex vaults (logical isolation scopes for notes and memories).

### `vault list`

```
memex vault list [OPTIONS]
```

List all available vaults. Also displays the currently active (write) vault and attached (read) vaults.

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |
| `--minimal` | bool | `False` | Output one vault name per line. |

---

### `vault create`

```
memex vault create NAME [OPTIONS]
```

Create a new vault.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `NAME` | Yes | Name for the new vault. |

#### Options

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--description` | `-d` | str | Optional description for the vault. |

#### Examples

```bash
memex vault create my-project --description "Notes for my-project development"
```

---

### `vault delete`

```
memex vault delete IDENTIFIER [OPTIONS]
```

Delete a vault by name or UUID.

#### Arguments

| Name | Required | Description |
|------|----------|-------------|
| `IDENTIFIER` | Yes | Name or UUID of the vault to delete. |

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--force` | `-f` | bool | `False` | Skip the confirmation prompt. |

> [!WARNING]
> Deleting a vault is destructive and removes all notes and memories within it.

---

## `server`

Manage the Memex Core API server.

### `server start`

```
memex server start [OPTIONS]
```

Start the Memex Core API server. Runs database readiness checks and schema initialization before starting.

* **Development mode** (`--reload`): Uses Uvicorn directly with auto-reload.
* **Production mode** (default): Uses Gunicorn with Uvicorn workers.

#### Options

| Option | Short | Type | Default | Env Var | Description |
|--------|-------|------|---------|---------|-------------|
| `--host` | | str | `0.0.0.0` | `MEMEX_HOST` | Host to bind the server to. |
| `--port` | | int | `8000` | `MEMEX_PORT` | Port to bind the server to. |
| `--workers` | `-w` | int | `2` | `MEMEX_WORKERS` | Number of Gunicorn worker processes (production mode only). |
| `--config` | `-c` | str | - | `MEMEX_CONFIG_PATH` | Path to configuration file. |
| `--reload` | | bool | `False` | - | Enable auto-reload for development (uses Uvicorn directly). |
| `--daemon` | `-d` | bool | `False` | - | Run in the background (production/Gunicorn mode only). |

#### Examples

```bash
# Start in development mode with auto-reload
memex server start --reload

# Start in production mode with 4 workers
memex server start --workers 4

# Start as a background daemon
memex server start --daemon

# Start on a custom port
memex server start --port 9000
```

> [!WARNING]
> `--daemon` is not supported with `--reload`. The daemon flag is ignored in development mode.

---

### `server stop`

```
memex server stop
```

Stop the running Memex Core API server. Sends SIGTERM, waits up to 10 seconds, then sends SIGKILL if needed.

---

### `server status`

```
memex server status
```

Check the status of the Memex Core API server. Verifies both the process (via PID file) and HTTP health (via the `/api/v1/metrics` endpoint).

---

## `mcp`

Manage the Model Context Protocol (MCP) server for LLM integration.

### `mcp run`

```
memex mcp run [OPTIONS]
```

Run the Memex MCP server.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--transport` | `-t` | str | `stdio` | Transport mode: `stdio` or `sse`. |
| `--host` | | str | `0.0.0.0` | Host for SSE transport. |
| `--port` | | int | `8000` | Port for SSE transport. |

#### Examples

```bash
# Run with stdio transport (default, for Claude Code / IDEs)
memex mcp run

# Run with SSE transport on a custom port
memex mcp run --transport sse --port 8080
```

> [!NOTE]
> Console logging is automatically suppressed in MCP mode to keep stdout clean for JSON-RPC communication.

---

## `stats`

View system-wide statistics and usage metrics.

### `stats system`

```
memex stats system [OPTIONS]
```

Show an overview of system counts: total memories (documents), entities, and reflection queue size.

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

### `stats tokens`

```
memex stats tokens [OPTIONS]
```

Show daily LLM token usage statistics.

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | bool | `False` | Output as JSON. |

---

## `config`

Manage Memex configuration.

### `config show`

```
memex config show [OPTIONS]
```

Display the current configuration. Secrets (passwords, API keys) are masked in the output.

Also shows the vault configuration summary with the source of the active vault setting (CLI flag, local config, global config, or default).

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--format` | `-f` | str | `yaml` | Output format: `yaml` or `json`. |
| `--compact` | | bool | `False` | Hide default values; show only user-set overrides. |

#### Examples

```bash
# Show full config in YAML
memex config show

# Show compact config as JSON
memex config show --format json --compact
```

---

### `config init`

```
memex config init [OPTIONS]
```

Initialize a new Memex configuration interactively. Prompts for required values (PostgreSQL connection, model name) and writes a YAML config file.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--path` | `-p` | Path | `~/.config/memex/config.yaml` | Path to write the configuration file. |

#### Examples

```bash
# Initialize with default path
memex config init

# Initialize at a custom path
memex config init --path ./my-memex-config.yaml
```

---

## `db`

Database schema migration commands. Wraps [Alembic](https://alembic.sqlalchemy.org/) for managing PostgreSQL schema versions.

### `db upgrade`

```
memex db upgrade [REVISION]
```

Run pending migrations up to the target revision.

#### Arguments

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `REVISION` | No | `head` | Target revision. Use `head` for the latest. |

#### Examples

```bash
# Upgrade to the latest schema
memex db upgrade

# Upgrade to a specific revision
memex db upgrade abc123
```

---

### `db downgrade`

```
memex db downgrade [REVISION]
```

Roll back migrations.

#### Arguments

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `REVISION` | No | `-1` | Target revision. Use `-1` to roll back one step. |

#### Examples

```bash
# Roll back one migration
memex db downgrade

# Roll back to a specific revision
memex db downgrade abc123
```

---

### `db current`

```
memex db current
```

Show the current migration revision applied to the database.

---

### `db history`

```
memex db history
```

Show the full migration history.

---

### `db stamp`

```
memex db stamp [REVISION]
```

Stamp the database with a revision without running migrations. Use this for existing databases created via `create_all` that already have the correct schema.

#### Arguments

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `REVISION` | No | `head` | Revision to stamp. |

---

### `db revision`

```
memex db revision [OPTIONS]
```

Generate a new Alembic migration script.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--message` | `-m` | str | `auto` | Migration message describing the change. |
| `--autogenerate / --no-autogenerate` | | bool | `True` | Auto-detect schema changes by comparing models to the database. |

#### Examples

```bash
# Auto-generate a migration from model changes
memex db revision --message "add webhooks table"

# Create an empty migration script
memex db revision --message "manual data migration" --no-autogenerate
```

---

## `setup`

Setup integrations with external tools.

### `setup claude-code`

```
memex setup claude-code [OPTIONS]
```

Configure Claude Code to use Memex as its long-term memory backend. Generates MCP server config, slash-command skills (`/remember`, `/recall`), lifecycle hooks, and optionally appends memory-integration instructions to `CLAUDE.md`.

#### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--project-dir` | `-p` | Path | `.` (current directory) | Target project directory. |
| `--vault` | `-v` | str | From config | Vault name to use. Defaults to the active vault from Memex config. |
| `--server-url` | | str | From config | Memex server URL override for the health check. |
| `--force` | `-f` | bool | `False` | Overwrite existing skill files and hooks. |
| `--no-claude-md` | | bool | `False` | Skip CLAUDE.md modifications. |
| `--no-hooks` | | bool | `False` | Skip hook generation. |
| `--with-session-tracking` | | bool | `False` | Include the `SessionEnd` hook for session tracking. |

#### Generated Files

| File | Description |
|------|-------------|
| `.mcp.json` | MCP server configuration for Claude Code. |
| `.claude/skills/remember/SKILL.md` | `/remember` slash command skill. |
| `.claude/skills/recall/SKILL.md` | `/recall` slash command skill. |
| `.claude/hooks/memex/*.sh` | Lifecycle hook scripts (SessionStart, PreCompact, PostToolUse, Stop). |
| `.claude/settings.local.json` | Claude Code settings with hook configuration. |
| `CLAUDE.md` (appended) | Memex memory integration instructions for the LLM. |

#### Examples

```bash
# Setup in the current project
memex setup claude-code

# Setup with a specific vault
memex setup claude-code --vault my-project

# Force overwrite existing files
memex setup claude-code --force

# Setup without modifying CLAUDE.md
memex setup claude-code --no-claude-md
```

---

## `report-bug`

```
memex report-bug
```

Open a pre-filled GitHub issue page in the default browser to report a bug. Automatically collects and attaches system information (Memex version, Python version, OS).
