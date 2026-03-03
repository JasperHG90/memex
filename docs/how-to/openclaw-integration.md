# How to Integrate Memex with OpenClaw

This guide shows you how to install, configure, and verify the Memex memory plugin for OpenClaw, giving your AI agents persistent long-term memory.

## Prerequisites

* Node.js >= 18.0.0
* An OpenClaw installation
* A running Memex server (`memex server start`)

## Instructions

### 1. Install the Plugin

Install the `memory-memex` plugin from the packages directory:

```bash
cd packages/openclaw
npm install
npm run build
```

Or download the package tarball from [GitHub Releases](https://github.com/JasperHG90/memex/releases) and install it:

```bash
npm install memory-memex-<version>.tgz
```

### 2. Register the Plugin

Add the plugin to your OpenClaw configuration file:

```json
{
  "plugins": [
    {
      "name": "memory-memex",
      "kind": "memory",
      "extensions": ["memory-memex"]
    }
  ]
}
```

### 3. Configure the Plugin

The plugin reads configuration from three sources (in order of precedence):

1. **Plugin config object** (passed by OpenClaw at registration)
2. **Environment variables**
3. **Built-in defaults**

**Minimal configuration via environment variables:**

```bash
export MEMEX_SERVER_URL=http://localhost:8000
export MEMEX_VAULT_NAME=my-project
```

**Full configuration via OpenClaw plugin config:**

```json
{
  "serverUrl": "http://localhost:8000",
  "vaultName": "my-project",
  "vaultId": null,
  "searchLimit": 8,
  "tokenBudget": null,
  "defaultTags": "agent,openclaw",
  "timeoutMs": 5000,
  "minCaptureLength": 50,
  "autoRecall": true,
  "autoCapture": true,
  "captureMode": "filtered",
  "profileFrequency": 20,
  "sessionGrouping": true
}
```

**Configuration reference:**

| Setting | Env Var | Default | Description |
| :--- | :--- | :--- | :--- |
| `serverUrl` | `MEMEX_SERVER_URL` | `http://localhost:8000` | Memex REST API URL |
| `vaultName` | `MEMEX_VAULT_NAME` | `OpenClaw` | Vault name for auto-created vaults |
| `vaultId` | `MEMEX_VAULT_ID` | `null` | Specific vault UUID (overrides name) |
| `searchLimit` | `MEMEX_SEARCH_LIMIT` | `8` | Max memory units to retrieve |
| `tokenBudget` | `MEMEX_TOKEN_BUDGET` | `null` | Token budget for retrieval (server default if null) |
| `defaultTags` | `MEMEX_DEFAULT_TAGS` | `agent,openclaw` | Comma-separated tags for captured notes |
| `timeoutMs` | `MEMEX_BEFORE_TURN_TIMEOUT_MS` | `5000` | Timeout for pre-turn recall (ms) |
| `minCaptureLength` | `MEMEX_MIN_CAPTURE_LENGTH` | `50` | Minimum user message length to trigger capture |
| `autoRecall` | -- | `true` | Inject relevant memories before each agent turn |
| `autoCapture` | -- | `true` | Store conversation turns after each agent turn |
| `captureMode` | `MEMEX_CAPTURE_MODE` | `filtered` | `filtered` (user only) or `full` (user + assistant) |
| `profileFrequency` | `MEMEX_PROFILE_FREQUENCY` | `20` | Fetch entity profile every N turns |
| `sessionGrouping` | `MEMEX_SESSION_GROUPING` | `true` | Group turns into session notes (vs. individual notes) |

### 4. Use Agent Tools

The plugin registers 9 tools that the agent can call during conversations:

| Tool | Description |
| :--- | :--- |
| `memex_memory_search` | Search long-term memories |
| `memex_add_note` | Save a new note to Memex |
| `memex_note_search` | Search source documents |
| `memex_read_note` | Read a full note by ID |
| `memex_get_page_index` | Get a note's table of contents |
| `memex_get_node` | Read a specific section of a note |
| `memex_get_lineage` | Trace a fact's provenance chain |
| `memex_list_entities` | Browse the knowledge graph |
| `memex_get_entity` | Get entity details and recent mentions |

### 5. Use Slash Commands

The plugin provides two slash commands for manual memory operations:

```
/recall <query>       Search Memex for relevant memories
/remember <text>      Store a text snippet in Memex
```

### 6. Use CLI Commands

The plugin adds commands to the OpenClaw CLI:

```bash
# Check Memex connectivity
openclaw memex status

# Search memories from the command line
openclaw memex search "deployment architecture" --limit 5
```

## Verification

1. **Check server connectivity:**

   ```bash
   openclaw memex status
   ```

   Expected output: `Memex server OK at http://localhost:8000`

2. **Test recall manually:**

   Start a conversation with your agent and use the slash command:

   ```
   /recall test query
   ```

   If Memex has content, you should see matching memory units.

3. **Verify auto-capture:**

   Have a conversation with the agent, then search for it:

   ```bash
   openclaw memex search "your conversation topic"
   ```

   The conversation should appear as a captured note.

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| "Cannot reach Memex" | Server not running or wrong URL | Start server, check `MEMEX_SERVER_URL` |
| No memories injected | Circuit breaker open after failures | Wait 60s for reset, then fix server connectivity |
| Conversations not captured | `autoCapture` is false or message too short | Check config, ensure messages exceed `minCaptureLength` (default: 50 chars) |
| Duplicate memories in context | Injected context not stripped | Update to latest plugin version (strips `<relevant-memories>` tags) |
| Agent tools not available | Plugin not registered correctly | Check OpenClaw plugin config and verify the extension path |

## See Also

* [About the OpenClaw Plugin](../explanation/openclaw-plugin.md) — plugin architecture, circuit breaker, prompt injection protection
* [Configuring Memex](configure-memex.md) — server-side configuration
* [Document Search vs. Memory Search](doc-search-vs-memory-search.md) — understanding `memex_memory_search` vs. `memex_note_search`
