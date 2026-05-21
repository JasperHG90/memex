# Configure the Hermes plugin

This guide wires Memex into a Hermes Agent deployment as the memory backend. It covers the install, the two places configuration lives, the choice between `hybrid`, `context`, and `tools` memory modes, and the checks that confirm Hermes actually loaded the plugin. It assumes you already have a running Memex server, a vault, and a working Hermes install.

## Prerequisites

- Memex server running and reachable. `memex server start -d` and confirm with `curl $MEMEX_SERVER_URL/healthz`.
- A vault to write into. List existing vaults with `memex vault list`; create one with `memex vault create my-vault`.
- Hermes Agent installed, with `$HERMES_HOME` exported (defaults to `~/.hermes`).
- `uv` on the path, for the `uv tool install` command below.

## Procedure

Configuration lives in two places. The plugin itself is installed under `$HERMES_HOME/plugins/memex/`. Settings live in `$HERMES_HOME/memex/config.json` (non-secrets) and `$HERMES_HOME/.env` (secrets); environment variables override both. Pick the recipes you need and apply them in order.

### 1. Install the plugin

Install the plugin package and run its installer:

```bash
uv tool install 'memex-hermes-plugin @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/hermes-plugin'
memex-hermes install
hermes memory setup    # pick "memex"
```

`memex-hermes install` writes `memory.provider: memex` to `$HERMES_HOME/config.yaml` and symlinks the plugin source into `$HERMES_HOME/plugins/memex/`. `hermes memory setup` then walks you through the four prompts in the plugin's config schema (server URL, API key, fallback vault, memory mode) and writes the answers to `$HERMES_HOME/memex/config.json`, with the API key going to `$HERMES_HOME/.env`.

To use a real copy instead of a symlink — useful in containers where symlinks across mounts break — pass `--mode copy`. Re-run with `--force` to replace an existing install.

### 2. Point the plugin at your Memex server

The plugin defaults to `http://127.0.0.1:8000`. If your server lives elsewhere — a remote machine, a different port, a devcontainer — pick one of three paths. They differ only in scope.

| Where you set it | Best for |
|---|---|
| `$HERMES_HOME/memex/config.json` (`server_url` key) | Stable per-machine setting |
| `MEMEX_SERVER_URL` env var | CI, containers, per-shell tweaks |
| `~/.config/memex/config.yaml` (`server_url` key) | You already run Memex locally and want one source of truth |

The plugin reads them in that order: env vars beat the JSON file, and the JSON file beats Memex's own config. If none of the three is set, you get the default.

For a secured Memex deployment, also set `MEMEX_API_KEY` (the value lands in the `X-API-Key` header on every request). Storing it as an env var keeps the secret out of the JSON file.

```bash
export MEMEX_SERVER_URL=https://memex.internal.example.com
export MEMEX_API_KEY=sk-...        # only for secured deployments
```

### 3. Bind a vault

The plugin resolves the vault to use per project. On session start, it derives a project ID from the git remote origin URL (or the directory path for non-git projects) and looks up the KV key `project:<project_id>:vault`. To bind a vault, run:

```bash
memex kv put "project:github.com/acme/myapp:vault" my-vault
```

Use `memex-hermes status` to print the derived project ID for the current directory if you are not sure what to put in the key.

If no per-project binding resolves, the plugin falls back to `vault_id` in `$HERMES_HOME/memex/config.json` (or `MEMEX_VAULT` if set), and finally to the Memex global default. When `create_vaults_on_init` is `true` (the default), a missing vault is auto-created on session start; set it to `false` if you want explicit creates only.

### 4. Pick a memory mode

The mode controls what the plugin injects into Hermes' system prompt, what it prefetches each turn, and which tools it exposes. Set it with the `memory_mode` config key or the `MEMEX_HERMES_MODE` env var.

| Mode | Briefing | Per-turn prefetch | Tools exposed |
|---|---|---|---|
| `hybrid` (default) | yes | yes | full surface (~38) |
| `context` | yes | yes | none |
| `tools` | no | no | primary 8 only |

`hybrid` is the right default for most deployments. `context` is for agents you want to read from Memex but never write to it (no tool dispatch). `tools` opts out of automatic context and hands the agent the eight primary verbs — `memex_memory_search`, `memex_note_search`, `memex_survey`, `memex_add_note`, `memex_append_note`, `memex_list_entities`, `memex_get_entity_mentions`, `memex_get_entity_cooccurrences` — and trusts it to compose retrieval explicitly. <code-ref path="packages/hermes-plugin/src/memex_hermes_plugin/memex/provider.py" lines="300-328" />

### 5. (Optional) Tune the briefing and prefetch

The defaults work; tune only when you need to. The settings below live in `$HERMES_HOME/memex/config.json`:

```json
{
  "server_url": "http://127.0.0.1:8000",
  "vault_id": "my-vault",
  "memory_mode": "hybrid",
  "briefing_budget": 2000,
  "briefing_refresh_cadence": 0,
  "recall": {
    "facts_limit": 5,
    "notes_limit": 3,
    "strategies": ["semantic", "keyword", "temporal", "graph", "mental_model"],
    "token_budget": 2048,
    "include_stale": false,
    "include_superseded": false,
    "expand_query": false
  },
  "retain": {
    "session_template": "hermes-session"
  }
}
```

`briefing_budget` accepts `1000` or `2000` only — the server validates other values away. <code-ref path="packages/hermes-plugin/src/memex_hermes_plugin/memex/config.py" lines="80-86" /> `briefing_refresh_cadence` of `0` means "fetch once at session start"; set it to `N` to refetch every N turns. Recall strategies must come from the TEMPR set; the validator rejects unknown names.

## Verification

After install, open a Hermes session and check three things.

**The provider loaded.** The session start log should show the briefing block under `## Memex Memory` in the system prompt, with the active vault and project ID on the second line. If the vault is unset, the block says `**No vault bound to this project.**` and prompts you to set the KV key. <code-ref path="packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py" lines="109-132" />

**The tools registered.** Ask the agent to list its available tools, or just ask for something memory-shaped — "what do you have on `<topic>`?". In `hybrid` mode you should see `memex_memory_search`, `memex_note_search`, and friends in the dispatch. In `context` mode the tools are intentionally hidden (the briefing is doing all the work). In `tools` mode you see exactly the primary eight.

**The transcript is being captured.** End the session. A note keyed `hermes:session:<ISO-timestamp>` should appear in your vault — confirm with `memex note list --vault my-vault` or by searching the title fragment. The capture is idempotent on the note key, so re-running a session does not duplicate.

You can also run `memex-hermes status` to print the resolved config, the derived project ID, and the install location without starting a Hermes session.

## Troubleshooting

**Tool calls return "Memex provider is not initialized."** Hermes called `handle_tool_call` before `initialize` succeeded. The most common cause is the Memex server being unreachable at session start — start it with `memex server start -d` and check `MEMEX_SERVER_URL`. If the server is up, look in the Hermes log for a `Vault resolution failed` line: a malformed `vault_id` in config can leave the provider half-initialized. <code-ref path="packages/hermes-plugin/src/memex_hermes_plugin/memex/provider.py" lines="330-342" />

**The briefing block is missing from the system prompt.** Three causes, in order of likelihood. First, `memory_mode` is `tools` — that mode skips the briefing by design. Second, no vault is bound and the project has no fallback — the briefing renders an empty block with a KV-bind hint instead. Third, the briefing fetch timed out (five seconds) because the server is slow or unreachable; check the Memex server logs.

**Tools missing in chat.** Check `memory_mode`. `context` hides the tools intentionally; switch to `hybrid` or `tools`. If you are in `tools` mode and expected the full surface, switch to `hybrid` — `tools` is the narrow eight-verb mode.

**Permission errors writing under `$HERMES_HOME/plugins/memex/`.** The install may have used `--mode copy` where a symlink was expected, or the reverse. Re-run `memex-hermes install --mode symlink --force` (or `--mode copy --force`) to repair.

**The agent writes to the wrong vault.** Check the resolution chain. Run `memex kv get "project:$(memex-hermes status --project-id):vault"` first; if that returns nothing, the fallback `vault_id` in `$HERMES_HOME/memex/config.json` is winning, or `MEMEX_VAULT` is set in the shell. Bind explicitly with `memex kv put` to make the resolution unambiguous.

## See also

- [Tutorial: Build an agent](../../tutorial/build-an-agent.md)
- [How-to: Configure the Claude Code plugin](claude-code.md)
- [Reference: configuration options](../../reference/configuration-options.md)
- [Explanation: session briefings](../../explanation/session-briefings.md)
