# Memex — Claude Code Plugin

Long-term memory for Claude Code, powered by [Memex](https://github.com/JasperHG90/memex).

## Why Memex?

Most AI memory systems are platform-controlled black boxes — the provider decides what to remember, how to store it, and what to surface. You have no visibility and no portability.

Memex takes the opposite approach. You own everything: the Postgres database, the markdown files, the vault structure. You decide what gets stored, how it's indexed, and when it's retrieved. You can inspect, export, or migrate your data at any time.

As AI assistants become more capable and long-lived, the question of who controls the memory becomes increasingly important. Memex keeps that control with you.

## Installation

### From the Memex marketplace

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

### From a third-party marketplace

To list this plugin in your own marketplace, add a `git-subdir` entry to your `marketplace.json`:

```json
{
  "name": "memex",
  "source": {
    "source": "git-subdir",
    "url": "https://github.com/JasperHG90/memex.git",
    "path": "packages/claude-code-plugin"
  },
  "description": "Long-term memory for Claude Code powered by Memex."
}
```

### Local development

```bash
claude --plugin-dir ./packages/claude-code-plugin
```

## What's included

- **Skills**: `/remember` and `/recall` slash commands for manual memory capture and retrieval
- **Hooks**: Automatic session lifecycle integration (startup context, pre-compaction reminders, post-commit capture prompts)
- **MCP Server**: Memex tools available as MCP tools (search, entities, notes, KV store)
- **Behavioral Instructions**: Injected at session start via `additionalContext` — covers proactive capture rules, retrieval routing, and citation requirements

## Prerequisites

1. Install the Memex CLI as a uv tool:

   ```bash
   uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
   ```

2. Initialize Memex and create a vault:

   ```bash
   memex config init
   memex vault create my-vault --description "My notes"
   ```

3. Start the Memex server (the plugin warns on session start if it's not running):

   ```bash
   memex server start -d
   ```

## Per-project vault binding

The plugin resolves which vault to use per project via the Memex KV store. The project identifier is derived from the git remote origin URL (portable across team members' machines), falling back to the directory name for non-git projects.

On session start, it looks up the KV key `claude-code:vault:<project_id>` — for example, `claude-code:vault:https://github.com/acme/myapp`.

To bind a project to a vault, ask Claude:

> Set this project's vault to "my-vault"

Or call the MCP tool directly:

```
memex_kv_write(key="claude-code:vault:https://github.com/acme/myapp", value="my-vault")
```

If no per-project vault is set, writes go to the default vault from your Memex config.

## Configuration

### Default vault

The plugin uses your existing Memex configuration. Set the global default vault via:

```bash
export MEMEX_VAULT__ACTIVE=my-vault
```

Or configure it in your `~/.memex.yaml`.

### Server URL

By default, the Memex CLI and hooks connect to `http://127.0.0.1:8000`. If your server runs elsewhere (e.g., in a devcontainer where the host is `host.docker.internal`), configure the URL using one of these methods, listed in priority order:

1. **`~/.config/memex/config.yaml`** (recommended) — covers both hooks and the MCP server:

   ```yaml
   server_url: http://host.docker.internal:8000
   ```

2. **Environment variable** — export in your shell profile (`.bashrc` / `.zshrc`):

   ```bash
   export MEMEX_SERVER_URL=http://host.docker.internal:8000
   ```

   This also covers both hooks and the MCP server.

3. **Plugin MCP env override** — set `MEMEX_SERVER_URL` in the project's `.mcp.json` env block. **Note:** this only affects the MCP server process, not hooks. Use option 1 or 2 if hooks also need the custom URL.

## Updating

When a new version is released, update the marketplace first, then the plugin:

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

This plugin replaces the per-project setup command. Remove the scaffolded files:

```bash
rm -rf .claude/skills/remember .claude/skills/recall
rm -rf .claude/hooks/memex
# Remove the memex entry from .mcp.json
# Remove the <!-- MEMEX CLAUDE CODE INTEGRATION --> section from CLAUDE.md
# Remove memex hooks from .claude/settings.local.json
```

Then install the plugin — it works across all projects automatically.
