# Memex -- Claude Code Plugin

Long-term memory for Claude Code, powered by [Memex](https://github.com/JasperHG90/memex).

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

- **Skills**: `/remember`, `/recall`, `/research`, `/explore`, `/digest` slash commands for memory capture, retrieval, deep research, entity exploration, and session summaries
- **Hooks**: Automatic session lifecycle integration (startup context, pre-compaction reminders, post-commit capture prompts)
- **Behavioral Instructions**: Injected at session start via `additionalContext` -- covers proactive capture rules, retrieval routing, search strategy selection, and citation requirements

### Architecture

The plugin communicates with the Memex server via direct HTTP calls using a thin helper module (`lib/memex_api.py`). This module wraps `RemoteMemexAPI` from `memex-common` and outputs JSON. Skills invoke it via `uv run --with memex-common`.

Each user's credentials are resolved from their own `MemexConfig` (`~/.config/memex/config.yaml` or env vars), providing per-user credential isolation and policy enforcement.

## Prerequisites

1. A running Memex server. Install and start it:

   ```bash
   uv tool install "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
   memex config init
   memex vault create my-vault --description "My notes"
   memex server start -d
   ```

   The plugin warns on session start if the server is not reachable.

2. `uv` must be available on `PATH` (the plugin uses `uv run --with memex-common` for dependency resolution).

## Skills

| Skill | Description |
|---|---|
| `/remember [text]` | Save information to long-term memory. Auto-selects a note template (quick note, technical brief, ADR, RFC, general note) based on content type. |
| `/recall [query]` | Search Memex for relevant facts, notes, and entities. Supports strategy selection (semantic, keyword, graph, temporal). |
| `/research [topic]` | Deep multi-step research: parallel search + entity graph walk + note reading + synthesis report. |
| `/explore [entity]` | Navigate the entity knowledge graph. Discover relationships and co-occurrences. |
| `/digest` | Summarize the session's key decisions, discoveries, and outcomes, then save to Memex. |

## Per-project vault binding

The plugin resolves which vault to use per project via the Memex KV store. The project identifier is derived from the git remote origin URL (portable across team members' machines), falling back to the directory name for non-git projects.

On session start, it looks up the KV key `project:<project_id>:vault`.

To bind a project to a vault, ask Claude:

> Set this project's vault to "my-vault"

Or run directly:

```bash
uv run --with memex-common python3 "${CLAUDE_PLUGIN_ROOT}/lib/memex_api.py" kv-write '{"key":"project:<project_id>:vault","value":"my-vault"}'
```

If no per-project vault is set, writes go to the default vault from your Memex config.

## Configuration

### Default vault

The plugin uses your existing Memex configuration. Set the global default vault via:

```bash
export MEMEX_VAULT__ACTIVE=my-vault
```

Or configure it in your `~/.config/memex/config.yaml`.

### Server URL

By default, the plugin connects to `http://127.0.0.1:8000`. If your server runs elsewhere (e.g., in a devcontainer where the host is `host.docker.internal`), configure the URL using one of these methods:

1. **`~/.config/memex/config.yaml`** (recommended):

   ```yaml
   server_url: http://host.docker.internal:8000
   ```

2. **Environment variable** -- export in your shell profile (`.bashrc` / `.zshrc`):

   ```bash
   export MEMEX_SERVER_URL=http://host.docker.internal:8000
   ```

### API key (optional)

For multi-user setups with ACL policies, each user configures their own API key:

```yaml
# ~/.config/memex/config.yaml
api_key: "your-api-key-here"
```

The key's policy (reader/writer/admin) and vault scope are enforced server-side.

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

Then install the plugin -- it works across all projects automatically.
