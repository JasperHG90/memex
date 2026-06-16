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

- **Skills**: slash commands for manual memory capture, retrieval, and curation.
  - `/remember`, `/recall`, `/retro` — capture by shape, search, and structured session postmortems.
  - `/extract-case` — turn an existing note, file, or URL into a case (gated to genuine how-tos).
  - `/procedure`, `/strategy` — recall a derived procedure, or the cross-procedure strategy for a verb, straight from the procedural plane.
  - `/case` — capture what you just did as a worked episode now (the system derives the procedure).
  - `/correct` — tell Memex a surfaced memory was wrong or stale (records `not_helpful` + deprioritizes).
  - `/handoff`, `/continue` — write a technical handoff summary of the current work as a tagged note, and pick it back up from the latest handoff in a later session.
- **Hooks**: Full session lifecycle integration:
  - `SessionStart` — installs rules (including `<project>/.claude/rules/memex-agent-surface.md`, the Tier 1b+2 agent surface auto-loaded into the system prompt), fetches a token-budgeted briefing (including a `## Procedures` block derived from the procedural plane so learned how-tos survive across sessions — recall via `memex_procedural_search`, write via `memex_case_submit`; there is no KV `procedure:` namespace), resolves the active vault, generates a per-session note key.
  - `SessionEnd` — auto-captures the full session transcript to long-term memory (safety net under `/remember`).
  - `PreCompact` — captures transcript-since-last-compact to the session note before context is discarded.
  - `UserPromptExpansion` — when `/recall` is invoked without arguments, composes a query from the last N transcript turns.
  - `PreToolUse` (on `memex_add_note` and `memex_case_submit`) — auto-injects ambient capture metadata (git, session, project, model, plugin version) and, for notes, defaults `background: true`.
  - `PostToolUse` (on `memex_add_note`, `Bash`, `Write`/`Edit`) — capture-counter, commit-nudge, edit-spiral nudge.
- **MCP Server**: Memex tools available as MCP tools (search, entities, notes, KV store)
- **Behavioral Instructions**: Delivered as a project rule file (`memex-agent-surface.md`, installed at `SessionStart`) so it loads into the system prompt directly rather than via the hook output channel — covers proactive capture rules, retrieval routing, and citation requirements. The dynamic per-vault briefing still rides on `additionalContext`.

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
memex_kv_put(key="app:claude-code:project:github.com/acme/myapp:vault", value="my-vault")
```

If no per-project vault is set, writes go to the default vault from your Memex config.

### Migration from the legacy namespace

Earlier plugin versions wrote to bare `project:<id>:vault` keys. The plugin now reads from `app:claude-code:project:<id>:vault` first; if absent, it falls back to the legacy key and forward-migrates the value (writing to the new key). The legacy key is left in place — KV deletions are user-initiated only.

## Configuration

The plugin is configured entirely through environment variables (set in your shell profile, your Claude Code `settings.json` `env` block, or — for MCP-only — the project's `.mcp.json`) and via `~/.config/memex/config.yaml` for the underlying Memex CLI.

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `MEMEX_SERVER_URL` | `http://127.0.0.1:8000` | Memex server URL used by both the hooks and the MCP server. Set this when your server runs elsewhere (e.g. `host.docker.internal` from a devcontainer). |
| `MEMEX_VAULT__ACTIVE` | unset | Server-side default active vault. Read by the Memex CLI's config layer (`~/.memex.yaml`). |
| `MEMEX_VAULT` | unset | Per-session vault override (rung 4 in the resolution chain — see "Per-project vault binding" below). Wins over the server default but loses to project/user/agent KV bindings. |
| `MEMEX_PLUGIN_VERSION` | `latest` | Pin the plugin's `uvx`-installed memex-cli to a specific git tag or branch (e.g. `v0.42.0`, `main`). Validated against `git ls-remote` with a 24h on-disk cache. |
| `MEMEX_LOCAL_PATH` | unset | **Dev mode** — point at a local Memex workspace checkout instead of `uvx`-installing from GitHub. Overrides `MEMEX_PLUGIN_VERSION`. Used by the eval suite to run Claude Code against the same code path Hermes runs. |
| `MEMEX_CC_AGENT_ID` | unset | Subagent identity. Enables the `app:claude-code:agent:<id>:vault` resolution rung (rung 3, between project and user). |
| `MEMEX_CC_TIMEOUT` | `8` (seconds, clamped to [1, 600]) | Hard timeout per `memex` CLI call from hooks. Protects SessionEnd/PreCompact from a hung server. |
| `MEMEX_CC_TRANSCRIPT_CAPTURE` | `on` | Toggle the SessionEnd + PreCompact transcript-capture hooks. Set to `off`/`0`/`false`/`no`/`disabled` to disable. Useful when transcripts are large and you don't want to pay the extraction cost. |
| `MEMEX_CC_SESSION_BRIEFING` | `on` | Toggle the SessionStart briefing injection. Set to `off`/etc. to disable. The vault-binding, session-note, and auto-tag instructions still emit — only the dynamic per-vault briefing markdown is suppressed. |
| `MEMEX_RESOLVE_VERBOSE` | unset | Internal — when `1`, the resolver emits uvx/version diagnostics as systemMessage. Hooks already set this where appropriate; users do not need to. |

> **Note on the `MEMEX_CC_*` toggles**: the parser only checks for the *falsy* values listed above (`off`, `0`, `false`, `no`, `disabled`). Anything else — including unsetting, leaving empty, or setting to `on`, `1`, `true`, or any other string — is treated as `on`. To re-enable a previously-disabled toggle, **unsetting** the variable is the cleanest path; explicitly setting to `on` also works. This is a deliberate one-way parser to keep the toggle logic five lines and avoid surprise enables under typo.

### How to disable behaviours

Paste into `~/.claude/settings.json` (or per-project `.claude/settings.local.json`):

```jsonc
{
  "env": {
    "MEMEX_CC_TRANSCRIPT_CAPTURE": "off",
    "MEMEX_CC_SESSION_BRIEFING": "off",
    "MEMEX_PLUGIN_VERSION": "v0.42.0"
  }
}
```

Or export in your shell profile for global effect across all Claude Code sessions:

```bash
export MEMEX_CC_TRANSCRIPT_CAPTURE=off
export MEMEX_CC_SESSION_BRIEFING=off
```

### `~/.config/memex/config.yaml` (server URL alternative)

Setting `MEMEX_SERVER_URL` in `~/.config/memex/config.yaml` is the cleanest way to point both hooks and the MCP server at a non-default server location:

```yaml
server_url: http://host.docker.internal:8000
```

This is preferred over the env var when the URL is stable for your machine.

### `.mcp.json` env override (MCP-only)

You can set `MEMEX_SERVER_URL` (and other env vars) in the project's `.mcp.json` env block, but **this only affects the MCP server process, not the hooks**. Use the env-var or config-file approach above when hooks also need the override.

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
