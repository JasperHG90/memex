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

- **Skills**: `/remember`, `/recall`, and `/retro` slash commands for manual memory capture, retrieval, and structured session postmortems.
- **Hooks**: Full session lifecycle integration:
  - `SessionStart` — installs rules, fetches a token-budgeted briefing, resolves the active vault, generates a per-session note key.
  - `SessionEnd` — auto-captures the full session transcript to long-term memory (safety net under `/remember`).
  - `PreCompact` — captures transcript-since-last-compact to the session note before context is discarded.
  - `UserPromptExpansion` — when `/recall` is invoked without arguments, composes a query from the last N transcript turns.
  - `PreToolUse` (on `memex_add_note`) — auto-injects ambient capture metadata (git, session, project, model, plugin version) and defaults `background: true`.
  - `PostToolUse` (on `memex_add_note`, `Bash`, `Write`/`Edit`) — capture-counter, commit-nudge, edit-spiral nudge.
- **MCP Server**: Memex tools available as MCP tools (search, entities, notes, KV store)
- **Behavioral Instructions**: Injected at session start via `additionalContext` — covers proactive capture rules, retrieval routing, and citation requirements

## Auto-capture safety net

The plugin captures every session as a Memex note keyed by `session:<timestamp>`. The capture is layered:

1. **PreCompact** — when Claude Code is about to compact context, the plugin appends transcript-since-last-compact to the session note. The first append creates the note; subsequent appends extend it in place.
2. **SessionEnd** — when the session ends (`reason ∈ {prompt_input_exit, logout, other}`), the plugin appends the remaining transcript. `clear` and `resume` events are skipped (the session is continuing).

Both layers share one `note_key` per CC session, so the final note is a single document with the full transcript — not a series of fragments. Server-side extraction processes the note's content into memory units regardless of how many appends happened.

This is a safety net, not a replacement for `/remember`. Use `/remember` for high-signal curated notes; rely on the auto-capture as background context the LLM can rediscover via `/recall`.

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

### Vault resolution chain

The plugin walks five sources in order, mirroring the Hermes plugin's chain:

1. `app:claude-code:project:<project_id>:vault` — per-project binding (most specific).
2. `app:claude-code:user:<$USER>:vault` — per-user default for this OS user.
3. `app:claude-code:agent:<MEMEX_CC_AGENT_ID>:vault` — per-subagent binding (only when the env var is set).
4. `MEMEX_VAULT` environment variable.
5. Memex server-side default (configured in `~/.memex.yaml`).

The first source that resolves to a non-empty value wins. Results are cached for the duration of the hook invocation.

### Binding a project

Ask Claude:

> Set this project's vault to "my-vault"

Or call the MCP tool directly:

```
memex_kv_write(key="app:claude-code:project:github.com/acme/myapp:vault", value="my-vault")
```

If no per-project vault is set, writes go to the default vault from your Memex config.

### Migration from the legacy namespace

Earlier plugin versions wrote to bare `project:<id>:vault` keys. The plugin now reads from `app:claude-code:project:<id>:vault` first; if absent, it falls back to the legacy key and forward-migrates the value (writing to the new key). The legacy key is left in place — KV deletions are user-initiated only.

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
