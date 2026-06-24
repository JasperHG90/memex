# Configure the Claude Code plugin

This guide covers the everyday tweaks: binding a vault to a project, turning hooks on or off, pointing the plugin at a non-default server, and pinning the Memex CLI version. The tutorial walks a first-time install; this page assumes you have already finished it and now want to change how the plugin behaves on a specific project.

## Prerequisites

- Memex server running (`memex server start -d`).
- Claude Code installed.
- The Memex plugin installed from the marketplace (`claude plugin install memex@memex`).
- A vault you want to use — list them with `memex vault list`.

## Procedure

The plugin is configured in two places: the Memex KV store (per-project vault binding) and Claude Code's `settings.json` `env` block (everything else). Pick the recipe you need.

### Know which slash commands the plugin adds

The plugin ships twelve skills. Each one is a slash command you type in Claude Code. <code-ref path="packages/claude-code-plugin/skills" lines="1" />

| Command | What it does |
|---|---|
| `/remember [text]` | Save to memory; routes by shape to a KV entry, a case, or a note. |
| `/recall [query]` | Search memory for facts, notes, and entities. Omit the query to compose one from the recent transcript. |
| `/case [what you did]` | File a worked episode (trigger / situation / actions / outcome / lesson) the system derives a procedure from. |
| `/extract-case [note\|file\|url]` | Turn existing content into a case — but only if it holds a reusable how-to. |
| `/procedure [task]` | Recall the step-by-step how-to for a task from the procedural plane. |
| `/strategy [verb]` | Recall the cross-procedure strategy — the general approach behind a verb. |
| `/correct [what's wrong]` | Tell Memex a surfaced memory was wrong or stale; records a `not_helpful` outcome and deprioritises it. |
| `/handoff` | Write a technical handoff summary of the current work so you can resume it later. |
| `/continue` | Resume from previous `/handoff` notes; pick from a list and load the relevant ones. |
| `/learnings` | Distill the session's durable learnings and route each into memory by shape (KV / case / note). |
| `/ingest [path\|url]` | Capture a local file or web page's content (and assets) into Memex as a note. |
| `/lint` | Review and resolve memory-hygiene findings — stale facts, duplicates, contradictions. |

### Bind a vault to this project

The plugin reads `app:claude-code:project:<project_id>:vault` from KV. The project ID is the normalised git remote URL — run `git remote get-url origin` and strip the scheme and `.git` suffix (for example, `git@github.com:acme/myapp.git` becomes `github.com/acme/myapp`).

From a shell:

```bash
memex kv put "app:claude-code:project:github.com/acme/myapp:vault" "my-vault"
```

Or ask Claude to do it for you:

> Set this project's vault to `my-vault`.

Claude will call `memex_kv_put` with the right key. The binding takes effect on the next Claude Code session start.

If you skip this step, writes go to the next rung in the resolution chain: `app:claude-code:user:<$USER>:vault`, then `app:claude-code:agent:<$MEMEX_CC_AGENT_ID>:vault` (only when a subagent identity is set), then `MEMEX_VAULT`, then the server-side default. <code-ref path="packages/claude-code-plugin/scripts/resolve_config.sh" lines="309-344" />

### Disable the session briefing

The `SessionStart` hook fetches a per-vault briefing and injects it into the system prompt. On large vaults this can add a second or two to startup. To turn it off, paste this into `~/.claude/settings.json` (global) or `<project>/.claude/settings.local.json` (per-project):

```jsonc
{
  "env": {
    "MEMEX_CC_SESSION_BRIEFING": "off"
  }
}
```

Or export from your shell profile:

```bash
export MEMEX_CC_SESSION_BRIEFING=off
```

Valid off-values are `off`, `0`, `false`, `no`, `disabled`. Anything else — including `on`, `1`, or any typo — leaves the briefing enabled. The vault-binding, session-note, and auto-tag instructions still emit; only the dynamic per-vault markdown is suppressed.

### Disable transcript capture

The `PreCompact` and `SessionEnd` hooks append the session transcript to a Memex note. To skip this — useful when you only want curated `/remember` notes and not the raw transcript:

```jsonc
{
  "env": {
    "MEMEX_CC_TRANSCRIPT_CAPTURE": "off"
  }
}
```

Same parser as the briefing toggle. The `/remember` and `/recall` skills still work; only the automatic transcript safety net is disabled.

### Know what the other hooks do

Beyond `SessionStart`, `PreCompact`, and `SessionEnd`, the plugin wires several smaller hooks. None has a toggle; they nudge or enrich rather than block. <code-ref path="packages/claude-code-plugin/hooks/hooks.json" lines="37-100" />

- **`UserPromptExpansion`** — when you run `/recall` with no query, composes one from the last few transcript turns.
- **`PreToolUse` on `memex_add_note` and `memex_case_submit`** — injects ambient capture metadata (git, session, project, model) and defaults `background=true`, so captures do not interrupt your turn.
- **`PostToolUse` on `Bash`** — nudges you to capture a commit worth remembering.
- **`PostToolUse` on `Write`/`Edit`** — surfaces an edit-spiral nudge when you keep editing the same file.
- **`PostToolUse` on `memex_add_note`** — increments a per-session capture counter the safety net reads.

### Point at a non-default Memex server

The plugin defaults to `http://127.0.0.1:8000`. If your server lives somewhere else — a devcontainer reaching `host.docker.internal`, a remote machine, a custom port — pick one of three paths. They differ in scope:

| Where you set it | Covers hooks | Covers MCP server |
|---|---|---|
| `~/.config/memex/config.yaml` (`server_url:` key) | yes | yes |
| `MEMEX_SERVER_URL` env var in shell profile or `settings.json` | yes | yes |
| `MEMEX_SERVER_URL` in the project's `.mcp.json` `env` block | no | yes |

Use the config file when the URL is stable for the machine. Use the env var when you want it scoped to one project. The `.mcp.json` route covers only the MCP server process — hooks still hit the default URL, which is almost never what you want.

YAML example:

```yaml
server_url: http://host.docker.internal:8000
```

### Which Memex CLI the plugin uses

The plugin's hooks and MCP server invoke the `memex` you installed yourself (`uv tool install "memex-cli[mcp,server] @ …"`, on PATH) — it does not build its own copy from a git ref. To change versions, upgrade your install: `uv tool upgrade memex-cli` (or re-run `uv tool install … --refresh` pinned to a specific tag).

If you want to point at a local Memex checkout (for plugin development), set `MEMEX_LOCAL_PATH` to the workspace path. The plugin then runs the CLI via `uv run --project` against your local code instead of the PATH install.

### Set a per-user default vault

When several projects on one machine should share a vault by default, write the binding at the user rung instead of the project rung:

```bash
memex kv put "app:claude-code:user:$USER:vault" "shared-vault"
```

Project bindings still win where they exist. This rung catches everything else before the chain falls through to `MEMEX_VAULT` and then the server default.

## Verification

After changing any setting, restart Claude Code and check the status line at session start. You should see one of:

- `🧠 Memex connected (vault: my-vault)` — vault resolved and briefing fetched.
- `🧠 Memex connected · Briefing disabled (MEMEX_CC_SESSION_BRIEFING)` — toggle is honoured.
- `🧠 Memex connected · No vault set — tell me which vault to use for this project` — no binding resolved; writes will go to the server default.

To confirm a vault binding from the shell:

```bash
memex kv get "app:claude-code:project:github.com/acme/myapp:vault"
```

To confirm the server URL the hooks will use, run `memex briefing --budget 1000` from a project directory and watch for connection errors.

## Troubleshooting

**Briefing markdown does not appear in the system prompt.** Check the status line. If it says `Briefing disabled`, an off-value is set in your environment or `settings.json`. If the status line is missing entirely, the server is unreachable — the hook emits a `Memex server is not reachable` message instead. Start the server with `memex server start -d` and restart Claude Code.

**The agent-surface rule file is stale or missing.** The hook writes `.claude/rules/memex-agent-surface.md` on each session start. If the file disappeared (someone ran `git clean`, or you deleted `.claude/`), the next session restores it. The status line will say `Agent surface installed at .claude/rules/memex-agent-surface.md — restart Claude Code to load it` — the file is on disk now, but Claude Code's system prompt was assembled before the hook ran, so the file binds on the next boot.

**A vault binding is ignored.** Check the resolution chain in order. Run `memex kv get "app:claude-code:project:<project_id>:vault"` with the exact normalised remote URL. If that returns nothing, check the user rung (`app:claude-code:user:$USER:vault`), then the agent rung (`app:claude-code:agent:$MEMEX_CC_AGENT_ID:vault`, set only for subagents), then the `MEMEX_VAULT` env var, then the server config. The first rung that returns a non-empty value wins; lower rungs are skipped. Legacy bare `project:<id>:vault` keys are read once and forward-migrated to the namespaced key on first session start.

## See also

- [Tutorial: Integrate Memex with Claude Code](../../tutorial/claude-code-integration.md)
- [How-to: Configure the Hermes plugin](hermes-plugin.md)
- [Reference: configuration options](../../reference/configuration-options.md)
- [Explanation: session briefings](../../explanation/session-briefings.md)
