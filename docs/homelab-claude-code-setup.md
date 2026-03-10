# Connecting Claude Code to Homelab Memex

How to configure Claude Code on any device to use the homelab Memex instance as long-term memory.

## Overview

There are two connection modes:

| Mode | When to use | Config |
|------|-------------|--------|
| **Local (SSE)** | Claude Code runs on the same machine as Memex | `url: http://localhost:8081/sse` |
| **Remote (Meshnet SSE)** | Claude Code runs on another device | `url: http://<meshnet-ip>:8081/sse` |

Both use the MCP SSE transport — no local Memex installation needed on the client.

## Option A: Automated Setup (Local Only)

If you have the Memex CLI installed locally:

```bash
cd your-project
memex setup claude-code --vault global
```

This generates `.mcp.json`, skills (`/remember`, `/recall`), hooks, and CLAUDE.md integration.

## Option B: Manual Setup (Local or Remote)

### Step 1: Create `.mcp.json` in your project root

```json
{
  "mcpServers": {
    "memex": {
      "url": "http://localhost:8081/sse"
    }
  }
}
```

For remote access via Meshnet, replace `localhost` with the Meshnet IP/hostname.

### Step 2: Add Memex instructions to CLAUDE.md

Append to your project's `CLAUDE.md`:

```markdown
## Memex (Long-Term Memory)

Access persistent memory via MCP tools. Build knowledge across sessions.

### When to save (call `memex_add_note`)
- After completing a multi-step task (save decisions + outcome)
- After diagnosing a bug (save symptom, cause, fix)
- After discovering an architectural pattern or user preference
- Keep notes concise (max 300 tokens). Use `background: true`, `author: "claude-code"`.

### When to search
- `memex_search` — find facts, observations, mental models
- `memex_note_search` — find source notes via hybrid retrieval

### Slash commands
- `/remember [text]` — save to memory
- `/recall [query]` — search memories
```

### Step 3: Create slash command skills (optional)

Create `.claude/skills/remember/SKILL.md`:

```markdown
---
name: remember
description: Save something to long-term memory
arguments:
  - name: text
    description: What to remember
    required: true
---

Call `memex_add_note` with:
- title: A concise title summarizing the memory
- markdown_content: "{{ text }}"
- description: A 1-sentence summary
- author: "claude-code"
- tags: Choose relevant tags
- background: true
```

Create `.claude/skills/recall/SKILL.md`:

```markdown
---
name: recall
description: Search long-term memory
arguments:
  - name: query
    description: What to search for
    required: true
---

Call `memex_search` with query="{{ query }}" and present the results.
If no results, try `memex_note_search` with query="{{ query }}".
```

### Step 4: Restart Claude Code

Restart Claude Code in the project directory so it picks up `.mcp.json`.

### Step 5: Verify

Ask Claude Code:

> "List all available vaults in Memex"

It should call `memex_list_vaults` and return vault names.

## Per-Project Vault Isolation

You can run separate vaults per project to keep memories isolated:

```json
{
  "mcpServers": {
    "memex": {
      "url": "http://localhost:8081/sse",
      "env": {
        "MEMEX_SERVER__ACTIVE_VAULT": "my-project"
      }
    }
  }
}
```

> **Note:** With SSE transport, env vars are passed to the MCP client, not the server. Vault selection with SSE may need to be handled via the API. Check if the MCP SSE bridge supports vault headers.

## Remote Access via NordVPN Meshnet

1. Enable Meshnet on both the homelab and the client device
2. Find the homelab's Meshnet hostname or IP:
   ```bash
   nordvpn meshnet peer list
   ```
3. Ensure port 8081 is allowed through Meshnet routing
4. Use the Meshnet address in `.mcp.json`:
   ```json
   {
     "mcpServers": {
       "memex": {
         "url": "http://homelab-hostname.nord:8081/sse"
       }
     }
   }
   ```

### Security for Remote Access

Before exposing Memex over Meshnet, enable API authentication:

Add to `docker-compose.override.yaml` under the `api` environment:

```yaml
MEMEX_SERVER__AUTH__ENABLED: "true"
MEMEX_SERVER__AUTH__API_KEYS__0: ${MEMEX_API_KEY}
```

Add `MEMEX_API_KEY=your-secret-key` to `.env`, then restart:

```bash
docker compose down && docker compose up -d
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Connection refused" | Check Memex is running: `docker compose ps` on the homelab |
| MCP tools not appearing | Verify `.mcp.json` is in project root; restart Claude Code |
| "No results found" | Vault may be empty — add a test note first |
| Slow responses | Ollama model loading can take a few seconds on first call |
| Meshnet unreachable | Check `nordvpn meshnet peer list` and firewall rules for port 8081 |

## See Also

- [Homelab Deployment](homelab-deployment.md) — server-side setup
- [Setup Claude Code](how-to/setup-claude-code.md) — automated setup with `memex setup claude-code`
- [Using MCP](how-to/using-mcp.md) — general MCP client configuration
