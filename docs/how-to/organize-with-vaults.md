# How to Organize Content with Vaults

This guide shows you how to create, manage, and switch between vaults to isolate your data by project, client, or purpose.

## Prerequisites

* A running Memex server
* Familiarity with the `memex` CLI

## Understanding Vault Roles

- **Global Vault**: Every Memex instance has a built-in `global` vault (ID: `ac9b6a45-d388-5ddb-9fa9-50d4e5bca511`). Data goes here when no vault is specified.
- **Write Vault**: The vault used for all write operations (ingestion, note creation). Resolved via `config.write_vault` (`vault.active` > `server.default_active_vault`).
- **Read Vaults**: Vaults included in search queries. Resolved via `config.read_vaults` (`vault.search` > `[vault.active]` > `[server.default_reader_vault]`).

## Instructions

### 1. Create a Vault

```bash
memex vault create "Project Hindsight" --description "Research on human-AI memory consolidation"
```

### 2. List Existing Vaults

```bash
memex vault list
```

This shows all vaults with their IDs and marks which one is currently active.

### 3. Switch the Active Vault

There is no `vault switch` command. Instead, specify the vault context in one of two ways:

**Per-command override (temporary):**

Use the `--vault` or `-v` flag on any ingestion or search command:

```bash
# Ingest into a specific vault
memex note add --url "https://example.com" --vault project-x

# Search across specific vaults
memex memory search "Status" --vault project-x --vault global
```

**Per-project configuration (persistent):**

Place a `.memex.yaml` in your project directory:

```yaml
vault:
  active: "project-x"
  search: ["project-x", "global", "reference-material"]
```

The `vault.active` setting is used for write operations. `vault.search` controls which vaults are included in search results.

> **Note:** `.memex.yaml` is reliable for the CLI (which runs in your shell's CWD), but MCP servers are spawned as subprocesses by AI clients — the working directory they inherit is not guaranteed to be your project root. If you use both CLI and MCP, prefer environment variables for consistent vault resolution. See [Configuring Memex — Vault Resolution for CLI and MCP](configure-memex.md#vault-resolution-for-cli-and-mcp) for details.

**Environment variable override (recommended for CLI + MCP consistency):**

```bash
export MEMEX_VAULT__ACTIVE=project-x
export MEMEX_VAULT__SEARCH='["project-x", "global"]'
```

### 4. Search Across Multiple Vaults

By default, searches query all vaults in `config.read_vaults`. To restrict or expand the scope:

```bash
# Search only in one vault
memex memory search "deployment" --vault project-x

# Search across multiple vaults explicitly
memex note search "architecture" --vault project-x --vault global
```

### 5. Delete a Vault

```bash
memex vault delete "Project Hindsight"
```

This is destructive — it removes the vault and all its contents (notes, memories, entities). Use `--force` to skip confirmation in scripts.

## Naming Conventions

Vault names should use only alphanumeric characters, hyphens, underscores, and dots. Memex warns if a vault name contains special characters.

Good names:
- `project-x`
- `client.acme`
- `research_2025`

Avoid:
- Names longer than 50 characters
- Spaces or special characters (`project x`, `client/acme`)

## Limitations

- Entities are global — they are not scoped to a single vault. An entity named "PostgreSQL" in vault A is the same entity in vault B.
- Vault deletion is hard-delete only. There is no archive or soft-delete.
- You cannot rename a vault after creation. Delete and recreate with the new name instead.

## Verification

To verify your vault setup, check the resolved configuration:

```bash
memex config show
```

Look for the `vault.active` and `vault.search` fields (or `server.default_active_vault` / `server.default_reader_vault`) to confirm they match your intent.

## See Also

* [Configuring Memex](configure-memex.md) — full configuration precedence
* [Deleting Content](delete-archival.md) — vault deletion cascade
* [About the Hindsight Framework](../explanation/hindsight-framework.md) — reflection operates within vault scope
