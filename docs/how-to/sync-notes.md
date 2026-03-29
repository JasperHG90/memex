# How to Sync Notes from a Local Folder

This guide shows you how to keep a folder of local notes (e.g., an Obsidian vault) synchronized with Memex using the `memex note sync` commands.

## Prerequisites

* A running Memex server (`memex server start`)
* The sync extra installed: `uv tool install "memex-cli[sync,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"`
* A folder of notes to sync (Markdown, PDF, DOCX, PPTX, XLSX, CSV, JSON, XML, HTML, MSG, or EML)

## Initialize the Config

Generate a default `note-sync.toml` in your vault folder:

```bash
memex note sync init ~/my-notes
```

This creates `~/my-notes/note-sync.toml` with sensible defaults. Edit it to customize behavior (see [Configuration](#configuration) below).

## First Sync

Run a full sync to import all notes into Memex:

```bash
memex note sync run ~/my-notes --full
```

The `--full` flag forces a complete scan. On the first run this is automatic, but it is useful if you ever need to re-sync everything from scratch.

Preview what would happen without making changes:

```bash
memex note sync run ~/my-notes --full --dry-run
```

## Incremental Sync

After the initial import, run sync without `--full` to process only changed files:

```bash
memex note sync run ~/my-notes
```

Memex tracks state in a local SQLite database (`.memex-sync.db` in your vault folder). Only new, modified, or deleted files are processed.

## Check Sync Status

See what has changed since the last sync:

```bash
memex note sync status ~/my-notes
```

This reports the current sync state, pending changes, and any files that would be processed on the next run.

## Watch Mode

For continuous synchronization, use watch mode. Memex monitors your folder and syncs changes automatically:

```bash
memex note sync watch ~/my-notes
```

Watch mode supports two backends:

| Mode | Flag | Description |
| :--- | :--- | :--- |
| Events | `--mode events` | File-system events via `watchdog` (default, low latency) |
| Poll | `--mode poll` | Periodic directory scan (works on network drives, NFS) |

```bash
# Use polling for network-mounted folders
memex note sync watch ~/my-notes --mode poll
```

## Background Batch Sync

For large vaults, submit sync as a background batch job so your terminal is not blocked:

```bash
memex note sync run ~/my-notes --background
```

This returns a job ID. Check progress with:

```bash
memex note sync job <job_id>
```

## Handling Deletes

When a file is removed from your vault, Memex can handle it in three ways:

| Behavior | Flag | Description |
| :--- | :--- | :--- |
| Archive (default) | _(none)_ | Deleted notes are archived in Memex and can be restored |
| Hard delete | `--hard-delete` | Permanently removes the note from Memex |
| Ignore | `--no-handle-deletes` | Deleted files are ignored; notes remain in Memex |

```bash
# Permanently remove notes when files are deleted
memex note sync run ~/my-notes --hard-delete

# Keep notes in Memex even if local files are removed
memex note sync run ~/my-notes --no-handle-deletes
```

## Skipping Files via Frontmatter

Add `agents: skip` to a note's YAML frontmatter to exclude it from sync:

```markdown
---
title: Private Draft
agents: skip
---

This note will not be synced to Memex.
```

The frontmatter key and value are configurable in `note-sync.toml` (see below).

## Configuration

Edit `note-sync.toml` in your vault folder to customize sync behavior. The full default config:

```toml
# vault_id = ""

[sync]
state_file = ".memex-sync.db"
batch_size = 32
note_key_prefix = "obsidian"
default_tags = ["obsidian"]
include_extensions = [
    ".md", ".pdf", ".docx", ".pptx", ".xlsx",
    ".csv", ".json", ".xml", ".html", ".msg", ".eml"
]

[sync.exclude]
base = [".obsidian", ".trash", ".git", "node_modules"]
extends_exclude = []
ignore_folders = []
frontmatter_skip_key = "agents"
frontmatter_skip_value = "skip"

[sync.assets]
enabled = true
max_size_mb = 50
extends_include = []

[watch]
mode = "events"
debounce_seconds = 5
poll_interval_seconds = 300
```

### Common customizations

**Change the vault target:**

```toml
vault_id = "my-project"
```

**Add extra folders to exclude:**

```toml
[sync.exclude]
extends_exclude = ["drafts", "templates"]
```

**Tag synced notes differently:**

```toml
[sync]
default_tags = ["notes", "obsidian"]
note_key_prefix = "vault-a"
```

**Tune watch mode polling interval:**

```toml
[watch]
poll_interval_seconds = 60
debounce_seconds = 2
```

Pass a custom config path if your TOML file is not in the vault folder:

```bash
memex note sync run ~/my-notes --config ~/configs/note-sync.toml
```

### Config precedence

Configuration is resolved in three layers (highest priority first):

1. **Environment variables** — prefixed with `MEMEX_SYNC_`
2. **TOML file** — `note-sync.toml`
3. **Built-in defaults**

## Error Handling

| Error | Cause | Fix |
| :--- | :--- | :--- |
| `ModuleNotFoundError: watchdog` | Sync extra not installed | Install with `uv tool install "memex-cli[sync]"` |
| Files skipped silently | Frontmatter contains `agents: skip` | Remove the frontmatter key or change `frontmatter_skip_key` in config |
| State database locked | Another sync process is running | Wait for it to finish, or check with `memex note sync status` |

## Verification

After syncing, confirm your notes are in Memex:

```bash
memex note list --vault my-project
```

Or check the sync state directly:

```bash
memex note sync status ~/my-notes
```

## See Also

* [Batch Ingestion](batch-ingestion.md) — one-time bulk import via CLI or API
* [Organizing with Vaults](organize-with-vaults.md) — vault isolation
* [Configuring Memex](configure-memex.md) — global configuration options
* [Delete and Archival](delete-archival.md) — how archival and hard deletion work
