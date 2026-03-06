# How to Set Up Claude Code Integration

This guide shows you how to configure Claude Code to use Memex as its long-term memory backend.

## Prerequisites

* Memex installed (`uv tool install memex-cli[mcp]`)
* A running Memex server (`memex server start -d`)
* Claude Code installed

## Instructions

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

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| "Vault not found" warning during setup | Create the vault first: `memex vault create <name>` |
| Hooks not firing | Check `.claude/settings.local.json` has the `hooks` key, then restart Claude Code |
| `/remember` or `/recall` not available | Verify `.claude/skills/remember/SKILL.md` exists; restart Claude Code |
| "Connection refused" in hooks | Ensure Memex server is running: `memex server start -d` |
| Stale CLAUDE.md section | Re-run with `--force` to replace the existing Memex section |

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info automatically attached.

## See Also

* [Using MCP](using-mcp.md) — manual MCP configuration for Claude Desktop, Cursor, and SSE transport
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for all 26 MCP tools
* [CLI Commands — setup claude-code](../reference/cli-commands.md#setup-claude-code) — all flags and options
