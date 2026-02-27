# Memex OpenClaw Memory Plugin (`@memex/openclaw-memory`)

A memory plugin for [OpenClaw](https://openclaw.dev) that gives your agent automatic recall and capture via Memex lifecycle hooks.

## What It Does

The plugin registers two OpenClaw lifecycle hooks:

- **`agent:beforeTurn`** (auto-recall) — searches Memex for memories relevant to the user's latest message and injects a summarized context block into the LLM prompt.
- **`agent:afterTurn`** (auto-capture) — stores each conversation turn as a Markdown note in Memex for future retrieval.

A built-in circuit breaker (3 failures → 60 s cooldown) ensures a degraded Memex server never blocks or slows down the agent.

## Prerequisites

- Node.js >= 18
- A running Memex server (`memex server start` or Docker)

## Build

```bash
cd packages/openclaw

# Install dependencies
npm install

# Compile TypeScript → dist/
npm run build

# Watch mode (recompiles on save)
npm run build:watch

# Remove compiled output
npm run clean
```

The build produces CommonJS modules, type declarations, and source maps under `dist/`.

## Test

Tests use [Vitest](https://vitest.dev).

```bash
# Single run
npm test

# Watch mode
npm run test:watch
```

## Configuration

All configuration is read from environment variables (the same ones used by the Memex MCP server):

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMEX_SERVER_URL` | `http://localhost:8000` | Base URL of the Memex REST API |
| `MEMEX_SEARCH_LIMIT` | `8` | Max memory results per recall query |
| `MEMEX_DEFAULT_TAGS` | `agent,openclaw` | Comma-separated tags applied to captured notes |
| `MEMEX_VAULT_ID` | *(none)* | Restrict search/capture to a specific vault |
| `MEMEX_BEFORE_TURN_TIMEOUT_MS` | `3000` | Timeout (ms) for the recall step |
| `MEMEX_MIN_CAPTURE_LENGTH` | `50` | Minimum user message length to trigger capture |

## Integration

### 1. Register with OpenClaw

Add the package to your OpenClaw agent's `package.json`:

```json
{
  "dependencies": {
    "@memex/openclaw-memory": "file:../packages/openclaw"
  }
}
```

The `openclaw.kind` field in the plugin's `package.json` tells the OpenClaw runtime that this is a `"memory"` plugin:

```json
{
  "openclaw": {
    "kind": "memory"
  }
}
```

### 2. Plugin Entry Point

The default export is a `registerPlugin` function that receives the OpenClaw `PluginContext`:

```typescript
import registerPlugin from '@memex/openclaw-memory';

// The OpenClaw runtime calls this automatically, but you can also
// invoke it manually for custom setups:
await registerPlugin(ctx);
```

### 3. Lifecycle Hook Flow

```
User message arrives
        │
        ▼
  ┌─────────────┐
  │ beforeTurn   │  Search Memex → summarize → injectContext()
  └──────┬──────┘
         │
         ▼
     LLM generates response
         │
         ▼
  ┌─────────────┐
  │ afterTurn    │  Format turn as Markdown → POST /api/v1/ingestions
  └─────────────┘
```

### 4. Memex API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/memories/search` | POST | Retrieve relevant memories (NDJSON stream) |
| `/api/v1/memories/summary` | POST | Summarize retrieved memories into a context block |
| `/api/v1/ingestions?background=true` | POST | Capture conversation turns (fire-and-forget) |

## Architecture

```
src/
├── index.ts            # Plugin entry point (registerPlugin, config resolution)
├── types.ts            # TypeScript interfaces (Memex DTOs + OpenClaw contracts)
├── memex-client.ts     # HTTP client for the Memex REST API
└── circuit-breaker.ts  # Circuit breaker to protect against server failures
```

## Documentation

- [Memex MCP Server](../mcp/README.md) — shared environment variables and server setup
