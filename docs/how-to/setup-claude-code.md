# How to Set Up Claude Code Integration

This guide shows you how to configure Claude Code to use Memex as its long-term memory backend.

## Recommended: Use the Claude Code Plugin

The easiest way to integrate Memex with Claude Code is via the marketplace plugin. It works across all projects automatically — no per-project setup needed.

```bash
# Add the marketplace (one-time)
claude plugin marketplace add JasperHG90/memex

# Install the plugin
claude plugin install memex@memex
```

See [packages/claude-code-plugin](../../packages/claude-code-plugin/) for details.

## Alternative: Per-Project Setup

If you prefer per-project configuration (e.g. to customize vaults or hooks per project), use the setup command below.

### Prerequisites

* Memex installed (`uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"`)
* A running Memex server (`memex server start -d`)
* Claude Code installed

### 1. Run the Setup Command

In your project directory, run:

```bash
memex setup claude-code
```

This generates all required files in one step:

| File | Purpose |
| :--- | :--- |
| `.mcp.json` | MCP server configuration connecting Claude Code to Memex |
| `.claude/skills/remember/SKILL.md` | `/remember` slash command — save context to Memex |
| `.claude/skills/recall/SKILL.md` | `/recall` slash command — search Memex memories |
| `.claude/hooks/memex/*.sh` | Lifecycle hooks (SessionStart, PreCompact, PostToolUse) |
| `.claude/settings.local.json` | Claude Code settings with hook registration |
| `CLAUDE.md` (appended) | Memory integration instructions for the LLM |

### 2. Specify a Vault (Optional)

By default, the setup uses your active vault from Memex config. To target a specific vault:

```bash
memex setup claude-code --vault my-project
```

### 3. Restart Claude Code

Start (or restart) Claude Code in the project directory so it picks up the new `.mcp.json` and hooks.

### 4. Verify the Connection

Ask Claude Code:

> "List all available vaults in Memex"

Claude should call `memex_list_vaults` and return your vault names. If this works, the integration is active.

### 5. Use Slash Commands

Try the generated slash commands:

- **`/remember`** — Save something to long-term memory: `/remember Always use single quotes in this project`
- **`/recall`** — Search memories: `/recall What coding conventions do we use?`

## What the Hooks Do

| Hook | Trigger | Action |
| :--- | :--- | :--- |
| `SessionStart` | Claude Code session begins | Searches Memex for relevant context and injects it |
| `PreCompact` | Context window is about to be compressed | Saves important context before it's lost |
| `PostToolUse (Bash)` | After a `git commit` | Captures commit details as a memory note |
| `PostToolUse (Write/Edit)` | After file writes/edits | Tracks significant file changes |

## Common Options

```bash
# Overwrite existing files (useful after Memex upgrades)
memex setup claude-code --force

# Skip CLAUDE.md modifications
memex setup claude-code --no-claude-md

# Skip hook generation (MCP + skills only)
memex setup claude-code --no-hooks

# Include session-end tracking
memex setup claude-code --with-session-tracking
```

## Server URL Configuration

By default, Memex connects to `http://127.0.0.1:8000`. If your server runs on a different host (common in devcontainers, remote setups, or Docker environments), configure the URL using one of these methods in priority order:

1. **`~/.config/memex/config.yaml`** (recommended):

   ```yaml
   server_url: http://host.docker.internal:8000
   ```

2. **Environment variable** in your shell profile (`.bashrc` / `.zshrc`):

   ```bash
   export MEMEX_SERVER_URL=http://host.docker.internal:8000
   ```

3. **`.mcp.json` env block** — only affects the MCP server, not hooks:

   ```json
   {
     "mcpServers": {
       "memex": {
         "env": { "MEMEX_SERVER_URL": "http://host.docker.internal:8000" }
       }
     }
   }
   ```

Options 1 and 2 cover both the MCP server and lifecycle hooks. Option 3 only covers the MCP server.

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| "Vault not found" warning during setup | Create the vault first: `memex vault create <name>` |
| Hooks not firing | Check `.claude/settings.local.json` has the `hooks` key, then restart Claude Code |
| `/remember` or `/recall` not available | Verify `.claude/skills/remember/SKILL.md` exists; restart Claude Code |
| "Connection refused" in hooks | Ensure the server is running (`memex server start -d`) and the URL is correct. If the server isn't at `http://127.0.0.1:8000` (e.g., in a devcontainer), set the URL via `~/.config/memex/config.yaml` (`server_url: http://host:port`), or export `MEMEX_SERVER_URL` in your shell profile. See the [plugin README](../../packages/claude-code-plugin/README.md#server-url) for details. |
| Stale CLAUDE.md section | Re-run with `--force` to replace the existing Memex section |

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info automatically attached.

## See Also

* [Using MCP](using-mcp.md) — manual MCP configuration for Claude Desktop, Cursor, and SSE transport
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for all MCP tools
* [CLI Commands — setup claude-code](../reference/cli-commands.md#setup-claude-code) — all flags and options
