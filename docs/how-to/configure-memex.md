# How to Configure Memex

This guide shows you how to set up and customize Memex configuration using YAML files, environment variables, and per-project overrides.

## Prerequisites

* Memex installed (`uv tool install git+https://github.com/JasperHG90/memex.git`)

## Configuration Sources and Precedence

Memex loads configuration from multiple sources. Later sources override earlier ones:

1. **Defaults** — built-in sensible defaults
2. **Global YAML** — `~/.config/memex/config.yaml` (Linux) or `~/Library/Application Support/memex/config.yaml` (macOS)
3. **Local YAML** — `.memex.yaml`, `memex_core.yaml`, or `memex_core.config.yaml` in the current directory (searched up to root)
4. **Environment variables** — prefixed with `MEMEX_`, nested with `__`

Environment variables always win. This means you can set defaults in YAML and override specific values per-session or in CI.

## Vault Resolution for CLI and MCP

If you use both the CLI and MCP (e.g., Claude Code), it's important to understand how each resolves the active vault — and why environment variables are the most reliable approach.

### Resolution Precedence

Both CLI and MCP follow the same precedence chain:

| Priority | Source | Example |
| :--- | :--- | :--- |
| 1 (highest) | Explicit parameter | `--vault` (CLI) or `vault_id` (MCP tool) |
| 2 | Environment variable | `MEMEX_VAULT__ACTIVE` |
| 3 | Local config file | `.memex.yaml` in CWD or parent dirs |
| 4 | Global config | `~/.config/memex/config.yaml` |
| 5 (lowest) | Server defaults | `server.default_active_vault` / `server.default_reader_vault` |

### Why Environment Variables Are Recommended

- **CLI** runs in your shell, so it always picks up `.memex.yaml` from the current working directory (or parent directories). This works reliably because you control where you run the command.
- **MCP** is spawned as a subprocess by your IDE or AI client. Whether the subprocess inherits your project's working directory depends on the client implementation — it is **not guaranteed**. This means `.memex.yaml` may or may not be found.
- **Environment variables** work identically for both CLI and MCP regardless of working directory. They are the recommended approach for consistent behavior.

### Setting Env Vars for MCP

Set vault env vars in your MCP server config (e.g., `.claude/mcp.json`):

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_VAULT__ACTIVE": "my-project",
        "MEMEX_VAULT__SEARCH": "[\"my-project\", \"shared\"]"
      }
    }
  }
}
```

> **Important:** `MEMEX_VAULT__SEARCH` must be a **string** containing a JSON array, not a native JSON array. Env vars are always strings — write `"[\"a\", \"b\"]"`, not `["a", "b"]`. The latter will fail MCP config validation. Pydantic-settings automatically JSON-decodes string env vars when the target field is a complex type like `list[str]`, so the string `'["a", "b"]'` becomes the Python list `["a", "b"]`. In a shell: `export MEMEX_VAULT__SEARCH='["my-project", "shared"]'`.

> **Tip:** `memex setup claude-code --vault my-project` generates this configuration automatically.

### Setting Env Vars for CLI

You have three options for the CLI:

1. **Shell profile** (always active) — add `export MEMEX_VAULT__ACTIVE=my-project` to `~/.bashrc` or `~/.zshrc`
2. **`.memex.yaml`** (per-project, picked up from CWD) — reliable since you control the working directory
3. **`--vault` flag** (per-command override) — `memex note add --vault my-project ...`

## Instructions

### 1. Set Up the Global Configuration

Create the global config file for settings that apply across all projects:

```bash
mkdir -p ~/.config/memex
cat > ~/.config/memex/config.yaml << 'EOF'
server:
  default_model:
    model: "gemini/gemini-3-flash-preview"
    api_key: "YOUR_API_KEY"
  meta_store:
    type: "postgres"
    instance:
      host: "localhost"
      port: 5432
      database: "memex"
      user: "postgres"
      password: "your-password"
EOF
```

### 2. Create a Per-Project Configuration

Place a `.memex.yaml` in your project root to override vault and model settings for that project:

```yaml
vault:
  active: "project-x"
  search: ["project-x", "global", "reference-material"]

server:
  memory:
    extraction:
      model:
        model: "openai/gpt-4o"
        api_key: "YOUR_OPENAI_KEY"
```

The `vault` section provides client-side overrides. `vault.active` sets the write target (overrides `server.default_active_vault`), and `vault.search` sets the read scope for search queries.

Memex searches the current directory and all parent directories for local config files, so you can place one at the repository root and it applies to all subdirectories.

### 3. Override Settings with Environment Variables

Use the `MEMEX_` prefix with `__` as the nesting delimiter:

```bash
# Override the server port
export MEMEX_SERVER__PORT=8081

# Override the client write vault
export MEMEX_VAULT__ACTIVE=my-project

# Override the client read vaults (JSON array)
export MEMEX_VAULT__SEARCH='["my-project", "shared"]'

# Override the extraction model
export MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL=ollama_chat/llama3

# Point to a specific config file
export MEMEX_CONFIG_PATH=/path/to/custom-config.yaml

# Disable config file loading entirely (useful for tests)
export MEMEX_LOAD_GLOBAL_CONFIG=false
export MEMEX_LOAD_LOCAL_CONFIG=false
```

### 4. Configure the LLM Model

Memex uses LiteLLM provider strings. Set the model in the `default_model` section — sub-components (extraction, reflection, document search) inherit it unless explicitly overridden:

```yaml
server:
  default_model:
    model: "gemini/gemini-3-flash-preview"
    api_key: "YOUR_KEY"
    temperature: 0.0

  memory:
    # Use a stronger model for reflection
    reflection:
      model:
        model: "openai/gpt-4o"
        api_key: "YOUR_OPENAI_KEY"
```

Supported provider prefixes include `gemini/`, `openai/`, `ollama_chat/`, `anthropic/`, and any provider supported by LiteLLM.

For local inference with Ollama:

```yaml
server:
  default_model:
    model: "ollama_chat/llama3"
    base_url: "http://localhost:11434"
```

### 5. Configure Retrieval and Reflection

```yaml
server:
  memory:
    retrieval:
      token_budget: 2000
      rrf_k: 60
      candidate_pool_size: 60
      temporal_decay_days: 30.0
      retrieval_strategies:
        semantic: true
        keyword: true
        graph: true
        temporal: true
        mental_model: true

    reflection:
      background_reflection_enabled: true
      background_reflection_interval_seconds: 600
      background_reflection_batch_size: 10
      max_concurrency: 3
      min_priority: 0.3
```

### 6. Configure Authentication and Rate Limiting

For production deployments:

```yaml
server:
  auth:
    enabled: true
    api_keys:
      - "your-secret-key-here"
    exempt_paths:
      - "/api/v1/health"
      - "/api/v1/ready"
  rate_limit:
    enabled: true
    ingestion: "10/minute"
    search: "60/minute"
    batch: "5/minute"
    default: "120/minute"
```

### 7. Configure Logging

```yaml
server:
  logging:
    level: "INFO"
    json_output: false
    log_file: "/var/log/memex/memex.log"
```

## Verification

To verify your configuration is loaded correctly:

```bash
memex config show
```

This prints the resolved configuration with all sources merged.

## Common Scenarios

| Scenario | Configuration |
| :--- | :--- |
| Local dev with Ollama | Set `default_model.model` to `ollama_chat/llama3`, `base_url` to `http://localhost:11434` |
| CI/CD pipeline | Use `MEMEX_` env vars, disable YAML loading with `MEMEX_LOAD_LOCAL_CONFIG=false` |
| Multi-project setup | One `.memex.yaml` per project root with different `vault.active` values |
| Production API | Enable `auth` and `rate_limit`, use PostgreSQL meta store |

## See Also

* [Organizing with Vaults](organize-with-vaults.md) — vault configuration
* [Configuration Reference](../reference/configuration.md) — full list of all settings
