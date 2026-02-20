# CLI Reference

The main command-line interface for the Memex system.

## Global Options
- `--config`, `-c`: Path to the configuration file.
- `--set`, `-s`: Override config values (e.g., `--set server.meta_store.instance.host=localhost`).
- `--debug`, `-d`: Enable debug logging.
- `--help`, `-h`: Show help message.

---

## Command Groups

### `server`
Manage the Memex Core API server.

- `server start`: Start the server.
    - `--host`: Host to bind to (default: 0.0.0.0).
    - `--port`: Port to bind to (default: 8000).
    - `--workers`, `-w`: Number of worker processes.
    - `--reload`: Enable auto-reload for development.
    - `--daemon`, `-d`: Run in the background (gunicorn only).
- `server stop`: Stop the running server.
- `server status`: Check server health and connection to database.

### `memory`
Ingest and search memories (facts and opinions).

- `memory add`: Add new content.
    - `content`: Text content to add (argument).
    - `--file`, `-f`: Path to a file or directory to ingest.
    - `--url`, `-u`: URL to scrape and ingest.
    - `--asset`, `-a`: Path to an asset file (image, PDF) to attach.
    - `--vault`, `-v`: Target vault for writing.
    - `--key`, `-k`: Unique stable key for the document.
- `memory search <query>`: Search knowledge base using TEMPR strategies.
    - `--vault`, `-v`: Filter by vault(s).
    - `--limit`: Number of results (default: 5).
    - `--token-budget`, `-t`: Max tokens for retrieval context.
    - `--answer`, `-a`: Generate an AI answer (default: True).
    - `--skip-opinions`: Skip automated opinion formation.
    - `--no-semantic`, `--no-keyword`, `--no-graph`, `--no-temporal`, `--no-mental-model`: Exclude specific search strategies.
    - `--json`: Output as JSON.
    - `--minimal`: Output unit IDs only.
- `memory delete <uuid>`: Delete a memory unit.
- `memory reflect`: Manually trigger a reflection cycle.
    - `entity_id`: Optional ID to reflect on a specific entity.
- `memory lineage <type> <id>`: Visualize the lineage of an entity.
    - `type`: one of `mental_model`, `observation`, `memory_unit`, `document`.
    - `--direction`, `-d`: `upstream` or `downstream`.
    - `--depth`: Max recursion depth.
    - `--limit`: Max children per node.
    - `--json`: Output as JSON.

### `document`
Manage and view raw source documents.

- `document list`: List all documents.
- `document recent`: Show most recent documents.
- `document search <query>`: Search raw documents using RRF fusion.
    - `--limit`, `-l`: Max results.
    - `--expand`: Enable query expansion.
    - `--blend`: Enable position-aware blending.
    - `--vault`, `-v`: Filter by vault(s).
    - `--reason`: Run skeleton-tree identification; shows relevant sections.
    - `--summarize`: Synthesize a full answer (implies --reason).
    - `--no-semantic`, `--no-keyword`, `--no-graph`, `--no-temporal`: Exclude specific search strategies.
    - `--json`: Output as JSON.
    - `--minimal`: Output document IDs only.
- `document view <id>`: View document content and metadata.
    - `--json`: Output as JSON.
- `document page-index <id>`: View the page index (slim tree) of a document.
    - `--json`: Output as JSON.
- `document node <id>`: View a specific page-index node (section) by its ID.
    - `--json`: Output as JSON.
- `document delete <id>`: Delete a document and its extracted memories.

### `entity`
Explore and manage extracted entities.

- `entity list`: List or search entities.
    - `--limit`, `-l`: Max entities to show.
    - `--query`, `-q`: Filter by name.
- `entity view <identifier>`: View details of an entity (Name or UUID).
- `entity mentions <identifier>`: Show memories mentioning this entity.
- `entity related <identifier>`: Show co-occurring entities.
- `entity delete <identifier>`: Delete an entity.
- `entity delete-mental-model <identifier>`: Delete a mental model in a vault.

### `vault`
Manage Memex Vaults (logical isolation).

- `vault list`: List all available vaults.
- `vault create <name>`: Create a new vault.
    - `--description`, `-d`: Optional description.
- `vault delete <identifier>`: Delete a vault (Name or UUID).

### `dashboard`
Manage the web-based knowledge graph interface.

- `dashboard start`: Start the Reflex dashboard.
    - `--host`, `--port`: Bind options.
    - `--dev`: Hot-reload mode.
- `dashboard stop`: Stop the dashboard.
- `dashboard status`: Check if the dashboard is running.

### `mcp`
Manage the Model Context Protocol (MCP) server.

- `mcp run`: Run the MCP server.
    - `--transport`, `-t`: `stdio` (default) or `sse`.

### `stats`
View system-wide statistics.

- `stats system`: Show counts of memories, entities, and reflection queue.
- `stats tokens`: Show daily LLM token usage.

### `config`
Manage Memex configuration.

- `config show`: Display current configuration (secrets masked).
    - `--format`: `yaml` (default) or `json`.
    - `--compact`: Hide default values.
- `config init`: Generate a default configuration file.
    - `--path`, `-p`: Where to write the file.
