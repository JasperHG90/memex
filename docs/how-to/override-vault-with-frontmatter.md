# How to Override the Target Vault via Frontmatter

Memex sync respects a `vault:` key in a note's YAML frontmatter as an explicit override of the sync's active vault. The override is per-note: it routes the note to a different vault on the same server without re-running sync for the whole folder.

This guide shows the typical flow: adding the override, re-syncing, and verifying the note migrated correctly.

## When to use this

Use a frontmatter override when:

* A single note belongs to a different project than the rest of the vault.
* You are reorganizing notes across vaults and want sync to migrate them rather than archive-and-recreate manually.
* You want one Obsidian folder to feed multiple Memex vaults based on per-note classification.

## Add the override

Edit the note's YAML frontmatter and add a `vault:` key. The value is either the vault's name or its UUID:

```markdown
---
title: Project Alpha notes
vault: alpha
---

# Meeting notes
...
```

## Re-sync

Run a normal incremental sync. Memex will detect the override, archive the prior version of the note in its original vault (if any), and re-ingest into the target:

```bash
memex note sync run ~/my-notes
```

The sync output reports a `migrated` counter alongside the usual ingest/skip/archive counts:

```
Synced 1 ingested, 0 skipped, 1 migrated
```

## Verify

Confirm the note now lives in the target vault:

```bash
memex note list --vault alpha
```

The original vault no longer surfaces the note in active retrieval — it was archived as part of the migration.

## Customizing the frontmatter key

By default, the sync engine reads the `vault:` key. Change it in `note-sync.toml` if your existing notes use a different convention:

```toml
[sync.exclude]
frontmatter_vault_key = "memex_vault"
```

## Edge cases

### Unknown vault name

If `vault:` cites a name (or UUID) that the server does not recognize, sync logs a warning and falls back to the active sync vault — the note is **not** orphan-created. Fix the typo or create the vault, then re-sync.

### Removing an override

Removing the `vault:` key from frontmatter is **not** automatically reverted. The note remains in its current target vault. To move it back, set `vault:` to the original vault explicitly, then re-sync.

### Vault name vs. UUID

Both work. Using the UUID is more stable across vault renames but less readable.

## See also

* [Sync Notes](sync-notes.md) — the canonical sync workflow.
* [Organize with Vaults](organize-with-vaults.md) — how vaults isolate notes and memories.
