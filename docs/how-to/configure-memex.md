# Configuring Memex

Memex is highly configurable via `config.yaml` or environment variables.

## Location

- **Global Config**: `~/.config/memex/config.yaml` (Linux), `~/Library/Application Support/memex/config.yaml` (macOS).
- **Project Config**: `.memex.yaml` in your project root.
- **Environment Variables**: Prefix with `MEMEX_` (e.g., `MEMEX_SERVER__PORT=8081`).

## Key Configuration Sections

### 1. Model Configuration (`model`)

Defines the LLM used for extraction and reflection.

```yaml
server:
  memory:
    extraction:
      model:
        model: "gemini/gemini-1.5-pro-latest"
        api_key: "YOUR_API_KEY"
        temperature: 0.0
```

- **`model`**: Provider string (e.g., `ollama/llama3`, `openai/gpt-4o`, `gemini/gemini-1.5-pro`).
- **`base_url`**: Optional URL for local inference (e.g., `http://localhost:11434` for Ollama).

### 2. Reflection Configuration (`reflection`)

Controls the "Hindsight" reflection loop.

```yaml
server:
  memory:
    reflection:
      background_reflection_enabled: true
      background_reflection_interval_seconds: 600
      model:
        model: "gemini/gemini-1.5-pro-latest" # Use a smarter model for reflection
```

- **`background_reflection_enabled`**: If true, Memex periodically synthesizes insights in the background.
- **`model`**: You can use a different (stronger) model for reflection than for extraction.

### 3. Retrieval Configuration (`retrieval`)

```yaml
server:
  memory:
    retrieval:
      token_budget: 2000
```

- **`token_budget`**: Max tokens to retrieve for a context window.

## Database Configuration

By default, Memex uses SQLite/DuckDB. For production, use PostgreSQL.

```yaml
server:
  meta_store:
    type: "postgres"
    instance:
      host: "localhost"
      user: "postgres"
      password: "password"
      database: "memex"
```
