# Organise content with vaults

Vaults are how Memex keeps one body of work apart from another. Notes, memory units, summaries, and reflections each carry a `vault_id`; queries filter on the active vault (or a wider read set you choose). Entities are the one exception: they stay global so the same person or place links across vaults.<code-ref path="DESIGN_DOCUMENT.md" lines="817-823" />

Use this guide when you want to give a project, client, or topic its own isolated slice — and still search across slices when you need to.

## Prerequisites

* A running Memex server.
* The `memex` CLI installed and pointed at that server (`memex config show` returns without error).

## Procedure

### 1. Create a vault

Run `memex vault create` with a name. A description is optional but worth writing — it appears in `memex vault list` and helps you (and agents) recall what the vault is for later.<code-ref path="packages/cli/src/memex_cli/vaults.py" lines="128-150" />

```bash
memex vault create project-hindsight \
  --description "Research on human-AI memory consolidation"
```

The CLI prints the new vault's UUID. Names should stay alphanumeric with dashes, underscores, or dots — spaces and slashes work poorly with the `--vault` flag.

### 2. List vaults and pick the active writer

Memex does not have a `vault switch` command. The active writer vault is read from your client config every time the CLI runs.<code-ref path="packages/common/src/memex_common/config.py" lines="2423-2435" />

Show what's available and what's currently active:

```bash
memex vault list
```

The table ends with two lines — `Active Vault (Write):` and `Read Vaults:` — that show the resolved values for the current shell.

To change the active writer vault, pick one of three scopes:

**One command at a time.** Pass `--vault` (or `-v`) on any write or search call:

```bash
memex note add --vault project-hindsight --title "Kickoff notes" "..."
```

**One project at a time.** Drop a `.memex.yaml` in the project root:

```yaml
vault:
  active: project-hindsight
  search:
    - project-hindsight
    - global
```

`vault.active` controls writes. `vault.search` controls which vaults reads draw from.

**One shell at a time.** Export the equivalent environment variables — handy when CLI and MCP need to agree on the vault (MCP servers inherit the parent shell's environment, not your project directory):

```bash
export MEMEX_VAULT__ACTIVE=project-hindsight
export MEMEX_VAULT__SEARCH='["project-hindsight", "global"]'
```

Precedence is `--vault` flag > env var > `.memex.yaml` > `server.default_active_vault`.

### 3. Read across more than one vault

By default, a search hits every vault listed in `vault.search`. If `vault.search` is unset, Memex falls back to `[vault.active]`; if that's also unset, it falls back to the server-side default reader vault.<code-ref path="packages/common/src/memex_common/config.py" lines="2428-2435" />

To widen a single search, list multiple `--vault` flags:

```bash
memex note search "deployment" --vault project-hindsight --vault global
```

To widen every search in a project, set `vault.search` in `.memex.yaml` to the full list.

### 4. Override the vault for one note via frontmatter

This override is **specific to `memex note sync`** — the folder-watching ingest path. It is not honoured by `memex note add`.<code-ref path="packages/cli/src/memex_cli/sync/engine.py" lines="135-208" />

Add a `vault:` key to the note's YAML frontmatter. The value can be a vault name or a vault UUID:

```markdown
---
title: Alpha kickoff
vault: project-alpha
---

# Meeting notes
...
```

Then run the usual sync:

```bash
memex note sync run ~/my-notes
```

Sync detects the override, archives the note's prior version in its old vault, and re-ingests it under the new one. The summary line reports a `migrated` counter:

```
Synced 1 ingested, 0 skipped, 1 migrated
```

If the configured frontmatter key isn't `vault`, change it in `note-sync.toml`:

```toml
[sync.exclude]
frontmatter_vault_key = "memex_vault"
```

### 5. Move an existing note to a different vault

For a note already in Memex (not a synced file), use `memex note migrate`. You pass the note's UUID and the target vault — name or UUID, either works:<code-ref path="packages/cli/src/memex_cli/notes.py" lines="912-946" />

```bash
memex note migrate 9f3c8b2a-... project-hindsight
```

The command moves the note and every memory unit, entity link, and reflection record tied to it. If the note already lives in the target vault, the CLI reports `noop` and exits without changes. Use `--force` to skip the confirmation prompt in scripts.

There is no bulk "move all notes from vault A to vault B" command — script `note migrate` over the IDs you want to move, or re-ingest with a frontmatter override and let sync handle the migration.

## Verification

Confirm the vault holds what you expect:

```bash
memex vault list           # Vault appears; note count column is non-zero
memex note search "kickoff" --vault project-hindsight
```

The search returns the note you just ingested. If nothing comes back, see Troubleshooting below.

You can also ask the server for its rendered vault summary — themes, key entities, recent activity:

```bash
memex vault summary project-hindsight
```

The first call may say *"No summary exists for this vault yet"*; pass `--regenerate` to build one on demand.<code-ref path="packages/cli/src/memex_cli/vaults.py" lines="251-294" />

## Troubleshooting

### `memex vault list` does not show the vault

The CLI is reading a different config (or a different server) than the one you created the vault on. Check:

```bash
memex config show
```

Compare `server_url` and the API key with the shell where `memex vault create` ran. A common cause is `MEMEX_SERVER_URL` set in one terminal but not the other.

### Search returns nothing from another vault

`vault.search` defaults to `[vault.active]`. A search you expected to span two vaults will only hit one until you widen the read set — either with repeated `--vault` flags on the command, or by listing both vaults in `.memex.yaml` under `vault.search`.

If you're running with auth enabled and the API key is scoped to a subset of vaults, the missing vault may simply be out of scope for that key. Re-run `memex vault list` and check the `Access` column.

### Frontmatter override appears to be ignored

Three things to verify, in order:

1. You ran `memex note sync run` — `note add` ignores the `vault:` frontmatter key. Only sync honours it.
2. The vault name (or UUID) in the frontmatter matches an existing vault. Sync logs a warning and falls back to the active sync vault for unknown values; `grep` the log for `Frontmatter vault override does not match`.
3. The frontmatter key matches `frontmatter_vault_key` in `note-sync.toml`. The default is `vault` — change one or the other so they agree.

Removing the `vault:` key afterwards does **not** move the note back. Sync remembers the previous routing in its per-file state. To return the note to its original home, set `vault:` explicitly to that vault and re-sync, or use `memex note migrate`.

## See also

* [Tutorial: Getting started](../tutorials/getting-started.md)
* [How-to: Sync notes](sync-notes.md)
* [Reference: Configuring Memex](configure-memex.md)
* [Explanation: The Hindsight framework](../explanation/hindsight-framework.md)
