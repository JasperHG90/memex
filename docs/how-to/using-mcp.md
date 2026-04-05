# How to Use the MCP Server

This guide shows you how to connect Memex to AI assistants via the Model Context Protocol (MCP), including Claude Desktop and other MCP-compatible clients.

## Prerequisites

* Memex installed with the MCP extra (`uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"`)
* A running Memex server (`memex server start`)
* An MCP-compatible AI client

## Progressive Disclosure

The MCP server supports **progressive disclosure** (opt-in) — when enabled, `tools/list` returns 3 discovery meta-tools instead of all 35 tool schemas. This reduces context window usage from ~5-8K tokens to ~100 tokens on connect.

The three discovery tools:

1. **`memex_tags`** — browse 7 tool categories with counts
2. **`memex_search(query, tags=[...])`** — find tools by keyword, optionally filtered by tag
3. **`memex_get_schema(tools=[...])`** — get parameter details for specific tools

Real tools remain directly callable by name — they are hidden from `tools/list` but available via `tools/call`. Set `MEMEX_MCP_PROGRESSIVE_DISCLOSURE=true` to enable progressive disclosure, or leave it unset to expose all 35 tools directly.

## Available MCP Tools

The Memex MCP server exposes 35 tools organized into 7 categories:

| Tag | Tools | Purpose |
| :--- | :--- | :--- |
| `search` | `memex_memory_search`, `memex_note_search`, `memex_find_note`, `memex_search_user_notes`, `memex_survey` | Search facts/notes, fuzzy title lookup, user annotations, broad surveys. Search results include `related_notes` and `links` for relationship discovery. |
| `read` | `memex_get_page_indices`, `memex_get_nodes`, `memex_get_notes_metadata`, `memex_read_note` | Read note content via TOC + sections. Page indices include `related_notes`. |
| `write` | `memex_add_note`, `memex_set_note_status`, `memex_rename_note`, `memex_update_user_notes`, `memex_get_template`, `memex_list_templates`, `memex_register_template` | Create/modify notes, user annotations, and templates |
| `browse` | `memex_list_notes`, `memex_recent_notes`, `memex_list_vaults`, `memex_active_vault`, `memex_get_vault_summary` | List notes, vaults, recent activity, vault summaries |
| `assets` | `memex_list_assets`, `memex_get_resources`, `memex_add_assets`, `memex_delete_assets` | Manage file attachments (images, PDFs) |
| `entities` | `memex_list_entities`, `memex_get_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences` | Knowledge graph exploration |
| `storage` | `memex_kv_write`, `memex_kv_get`, `memex_kv_search`, `memex_kv_list`, `memex_get_memory_units`, `memex_get_lineage` | KV store, memory units, lineage |

## Instructions

### Configure for Claude Desktop

Add the following to your `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_VAULT__ACTIVE": "global"
      }
    }
  }
}
```

All vault parameters on MCP tools are optional and default to the resolved config values. Set `MEMEX_VAULT__ACTIVE` to control the write vault and `MEMEX_VAULT__SEARCH` (JSON array) to control read scope.

### Configure for Claude Code

The recommended way to use Memex with Claude Code is via the plugin. See [How to Set Up Claude Code Integration](setup-claude-code.md) for installation and configuration.

If you need manual MCP configuration without the plugin, add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_VAULT__ACTIVE": "my-project"
      }
    }
  }
}
```

### Vault Configuration for MCP

MCP servers are spawned as subprocesses by your AI client (Claude Desktop, Claude Code, etc.). Unlike the CLI — which runs in your shell and reliably finds `.memex.yaml` from your working directory — MCP subprocesses are **not guaranteed** to inherit your project's CWD. This means a `.memex.yaml` in your project root may not be found by the MCP server.

For this reason, always set vault configuration via environment variables in the MCP server config:

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex", "mcp", "run"],
      "env": {
        "MEMEX_VAULT__ACTIVE": "my-project",
        "MEMEX_VAULT__SEARCH": "[\"my-project\", \"shared\"]"
      }
    }
  }
}
```

> **Important:** `MEMEX_VAULT__SEARCH` must be a **string** containing a JSON array, not a native JSON array. Env vars are always strings — write `"[\"a\", \"b\"]"`, not `["a", "b"]`. The latter will fail MCP config validation. Pydantic-settings automatically JSON-decodes string env vars when the target field is a complex type like `list[str]`, so the string `'["a", "b"]'` becomes the Python list `["a", "b"]`.

For the full vault resolution precedence (shared by CLI and MCP), see [Configuring Memex — Vault Resolution for CLI and MCP](configure-memex.md#vault-resolution-for-cli-and-mcp).

### Use SSE Transport (Remote Server)

For remote or shared deployments, run the MCP server in SSE mode instead of stdio:

```bash
memex mcp run --transport sse --host 0.0.0.0 --port 8000
```

Then configure your client to connect via SSE instead of spawning a subprocess:

```json
{
  "mcpServers": {
    "memex": {
      "url": "http://your-server:8000/sse"
    }
  }
}
```

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| "memex_mcp is not installed" | Missing MCP extra | Run `uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"` |
| Tools not appearing in client | Config file in wrong location | Check the path for your OS (see above) |
| "Connection refused" errors | Memex server not running | Start with `memex server start` |
| Wrong vault in results | `MEMEX_VAULT__ACTIVE` not set | Add the env var to your MCP config |
| Slow tool responses | Large result sets | Reduce `limit` parameter or set `token_budget` |
| "No results found" | Empty vault or unprocessed notes | Check `memex note list` and wait for extraction to complete |

## Verification

After configuring, verify the connection by asking your AI assistant:

> "List all available vaults in Memex"

The assistant should call `memex_list_vaults` and return your vault names. If this works, all MCP tools are accessible.

## Best Practices for AI Agents

- **Search before answering**: Use `memex_memory_search` to ground responses in stored knowledge.
- **Use templates for consistency**: Call `memex_get_template` before saving structured notes (ADRs, tech briefs).
- **Check the active vault**: Call `memex_list_vaults` before writing to confirm the target vault (`memex_active_vault` is deprecated).
- **Prefer page index over full reads**: Use `memex_get_page_indices` then `memex_get_nodes` instead of `memex_read_note` for large notes.

## See Also

* [Configuring Memex](configure-memex.md) — environment variables and YAML settings
* [MCP Tools Reference](../reference/mcp-tools.md) — full parameter documentation for each tool
* [Document Search vs. Memory Search](doc-search-vs-memory-search.md) — choosing between `memex_memory_search` and `memex_note_search`
