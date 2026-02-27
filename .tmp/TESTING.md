# Testing the Memex OpenClaw Plugin

## Prerequisites

- OpenClaw installed globally: `npm install -g openclaw@latest`
- Memex server running: `memex server start -d` (default: `http://localhost:8000`)
- PostgreSQL with pgvector backing the Memex server

## Install the Plugin

```bash
openclaw plugins install -l /home/vscode/workspace/packages/openclaw
```

This:
- Registers `memory-memex` as the active memory plugin
- Disables the built-in `memory-core` and `memory-lancedb`
- Adds the plugin path to `plugins.load.paths` in `~/.openclaw/openclaw.json`

## Verify Installation

```bash
# Plugin should show as "loaded"
openclaw plugins list | grep memex

# Should print "Memex server OK at http://localhost:8000"
openclaw memex status
```

## CLI Commands

```bash
# Search memories
openclaw memex search "your query here"

# Search with a result limit
openclaw memex search "your query here" --limit 5

# Check server connectivity
openclaw memex status
```

## Talk to the Agent (see memory in action)

The plugin hooks into the agent lifecycle:
- **`before_agent_start`** — searches Memex and injects relevant memories into context
- **`agent_end`** — captures the conversation turn to Memex

### 1. Start the Gateway

```bash
openclaw gateway --allow-unconfigured --auth none --log-level info
```

### 2. Set the model to Gemini (uses GEMINI_API_KEY from env)

```bash
openclaw config set agents.defaults.model google/gemini-2.5-flash
```

### 3. Run an agent turn

```bash
# Basic turn — auto-recall injects memories, agent responds, auto-capture stores the turn
openclaw agent --agent main -m "What do you know about Andrew?"

# With JSON output to inspect tools, token usage, and memory injection
openclaw agent --agent main -m "What do you know about Andrew?" --json

# Ask the agent to use tools explicitly
openclaw agent --agent main -m "Use your memex_search tool to find information about recent events"

# With a timeout (seconds)
openclaw agent --agent main -m "Summarize what you remember" --timeout 120
```

### What to look for in `--json` output

- `tools.entries` should list all 9 `memex_*` tools
- `usage.input` will be large (>10k tokens) if auto-recall injected memories
- `result.payloads[0].text` should reference information only available in Memex

### Gateway logs

Check `/tmp/openclaw/openclaw-*.log` or the gateway terminal for:
```
memory-memex: injecting 8 memories into context
memory-memex: conversation captured
```

## Important: `openclaw memory` vs `openclaw memex`

These are **two different systems**:

| Command | System | Description |
|---------|--------|-------------|
| `openclaw memory status` | Built-in workspace memory | Indexes local files into SQLite. Always present. |
| `openclaw memory search <q>` | Built-in workspace memory | Searches the local file index. |
| `openclaw memex status` | Our plugin | Checks connectivity to the Memex REST API. |
| `openclaw memex search <q>` | Our plugin | Searches Memex long-term memory via the REST API. |

The built-in `openclaw memory` is a core feature that indexes your project files locally.
Our plugin works through the **agent lifecycle hooks** and **agent tools** during conversations.

## Unit Tests

```bash
cd /home/vscode/workspace/packages/openclaw
npm test              # 148 tests across 5 files
npm run test:watch    # Watch mode
```

## Uninstall / Switch Back

```bash
openclaw plugins disable memory-memex
openclaw plugins enable memory-core
```
