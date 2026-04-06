# How to Set Up Claude Code Integration

This guide shows you how to add long-term memory to Claude Code using the Memex plugin.

## Prerequisites

* Memex CLI installed:

  ```bash
  uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
  ```

* Memex initialized with a vault:

  ```bash
  memex config init
  memex vault create my-vault --description "My notes"
  ```

* A running Memex server:

  ```bash
  memex server start -d
  ```

* Claude Code installed

## Install the Plugin

From your terminal:

```bash
# Add the marketplace (one-time)
claude plugin marketplace add JasperHG90/memex

# Install the plugin
claude plugin install memex@memex
```

Or from inside Claude Code:

```
/plugin marketplace add JasperHG90/memex
/plugin install memex@memex
```

The plugin works across all projects automatically — no per-project setup needed.

## Verify the Connection

Start (or restart) Claude Code and ask:

> "List all available vaults in Memex"

Claude should call `memex_list_vaults` and return your vault names. If this works, the integration is active.

## Bind a Vault to Your Project

The plugin resolves which vault to use per project via the Memex KV store. The project identifier is derived from the git remote origin URL (portable across machines), falling back to the directory name for non-git projects.

To bind a project to a vault, ask Claude:

> "Set this project's vault to my-vault"

Or call the MCP tool directly:

```
memex_kv_write(key="claude-code:vault:https://github.com/acme/myapp", value="my-vault")
```

If no per-project vault is set, writes go to the default vault from your Memex config.

## Skills

| Skill | Usage | What it does |
| :--- | :--- | :--- |
| `/remember` | `/remember [text]` | Saves the provided text (or infers the most important context from the conversation) as a persistent note |
| `/recall` | `/recall [query]` | Searches memories, facts, notes, and entities for relevant information |

## Hooks

The plugin registers lifecycle hooks that fire automatically during your session:

| Hook | Trigger | Action |
| :--- | :--- | :--- |
| `SessionStart` | Claude Code session begins | Searches Memex for relevant context and injects it along with behavioral instructions |
| `PreCompact` | Context window is about to compress | Reminds Claude to persist important context before compaction |
| `PostToolUse (Bash)` | After a `git commit` | Prompts capture of commit details as a memory note |
| `PostToolUse (Write/Edit)` | After file writes/edits | Prompts capture of significant changes (throttled to every 10th invocation) |

Hooks are managed by the plugin — no manual configuration needed.

## Configuration

### Default Vault

The plugin uses your existing Memex configuration. Set the global default vault via:

```bash
export MEMEX_VAULT__ACTIVE=my-vault
```

Or configure it in your `~/.memex.yaml`.

### Server URL

By default, Memex connects to `http://127.0.0.1:8000`. If your server runs on a different host (common in devcontainers, remote setups, or Docker environments), configure the URL using one of these methods in priority order:

1. **`~/.config/memex/config.yaml`** (recommended) — covers both hooks and the MCP server:

   ```yaml
   server_url: http://host.docker.internal:8000
   ```

2. **Environment variable** in your shell profile (`.bashrc` / `.zshrc`) — also covers both:

   ```bash
   export MEMEX_SERVER_URL=http://host.docker.internal:8000
   ```

3. **Plugin MCP env override** — set `MEMEX_SERVER_URL` in the project's `.mcp.json` env block. **Note:** this only affects the MCP server process, not hooks. Use option 1 or 2 if hooks also need the custom URL.

### Session Briefing

The session briefing provides a token-budgeted knowledge index at the start of each Claude Code session. It composes vault summaries, top entities with mental model trend indicators, KV facts, and available vaults into a single markdown document.

The Claude Code plugin runs `memex session` automatically via a hook at session start. You can also run it manually:

```bash
# Default 2000-token budget
memex session

# Smaller budget for faster startup
memex session --budget 1000
```

The briefing includes:

- **Vault summary** — topics, themes, and stats for the active vault
- **Top entities** — most relevant entities with trend indicators (new/stable/strengthening/weakening/stale)
- **KV facts** — key-value entries from the active vault's namespace
- **Available vaults** — all accessible vaults with descriptions

The budget controls total output size. When content exceeds the budget, lower-priority sections are truncated or omitted.

## Update the Plugin

When a new version is released:

```bash
claude plugin marketplace update JasperHG90/memex
claude plugin update memex@memex
```

Or from inside Claude Code:

```
/plugin marketplace update JasperHG90/memex
/plugin update memex@memex
```

## Migrating from `memex setup claude-code`

If you previously used the per-project `memex setup claude-code` command, remove the scaffolded files before installing the plugin:

```bash
rm -rf .claude/skills/remember .claude/skills/recall
rm -rf .claude/hooks/memex
# Remove the memex entry from .mcp.json
# Remove the <!-- MEMEX CLAUDE CODE INTEGRATION --> section from CLAUDE.md
# Remove memex hooks from .claude/settings.local.json
```

Then install the plugin as described above.

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| Plugin not appearing after install | Restart Claude Code |
| "Connection refused" from hooks or MCP | Ensure the server is running (`memex server start -d`). If not at `127.0.0.1:8000`, set the URL via `~/.config/memex/config.yaml` or `MEMEX_SERVER_URL`. |
| `/remember` or `/recall` not available | Verify plugin installed: `claude plugin list`; reinstall if missing |
| Wrong vault in results | Check vault binding: ask Claude "What vault is this project using?" or verify the KV key |
| Session start context missing | Server must be running before the session starts; restart Claude Code after starting the server |

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info automatically attached.

## See Also

* [Using MCP](using-mcp.md) — manual MCP configuration for Claude Desktop and SSE transport
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for all MCP tools
* [Configuring Memex](configure-memex.md) — environment variables and YAML settings
