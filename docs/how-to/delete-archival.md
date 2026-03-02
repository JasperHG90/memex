# How to Delete Content

This guide shows you how to delete notes, memory units, entities, mental models, and vaults from Memex. All delete operations are **permanent** and trigger cascading cleanup of derived data.

## Prerequisites

* A running Memex server
* The UUID of the item you want to delete (find it with `memex note list`, `memex memory search`, or `memex entity list`)

## Instructions

### Delete a Note

Deleting a note removes the note and all data derived from it.

```bash
memex note delete <note_uuid>
```

**Cascade effect:**

1. The note record is removed from the database.
2. The file content and assets are deleted from the FileStore (disk).
3. All text chunks associated with the note are deleted.
4. All memory units (facts, events) derived solely from this note are deleted.
5. Entity links from the deleted units are removed. Entities themselves are **not** deleted, as they may be referenced by other notes.

To skip the confirmation prompt in scripts:

```bash
memex note delete <note_uuid> --force
```

> **Warning:** There is no undo. If you need the content later, export the note first with `memex note view <note_uuid>`.

### Delete a Memory Unit

Remove a single memory unit without deleting the source note:

```bash
memex memory delete <unit_uuid>
```

**Cascade effect:**

1. The memory unit is removed.
2. Entity links and memory links for that unit are deleted.

To skip confirmation:

```bash
memex memory delete <unit_uuid> --force
```

### Delete an Entity

Remove an entity and all its associated metadata:

```bash
memex entity delete <name_or_uuid>
```

**Cascade effect:**

1. The entity record is removed.
2. All mental models for the entity are deleted.
3. All aliases, entity links, and co-occurrence records are removed.

To skip confirmation:

```bash
memex entity delete <name_or_uuid> --force
```

### Delete a Mental Model Only

Remove the synthesized mental model for an entity without deleting the entity itself:

```bash
memex entity delete-mental-model <name_or_uuid>
```

Optionally scope to a specific vault:

```bash
memex entity delete-mental-model <name_or_uuid> --vault <vault_uuid>
```

The entity and its underlying memory units remain intact. The reflection engine will regenerate the mental model in a future cycle if background reflection is enabled.

### Delete a Vault

Deleting a vault removes **everything** inside it.

```bash
memex vault delete <name_or_uuid>
```

**Cascade effect:**

1. All notes in the vault are deleted (triggering their own cascades).
2. All memory units scoped to the vault are deleted.
3. All entities specific to the vault are removed.
4. The vault definition itself is deleted.

To skip confirmation:

```bash
memex vault delete <name_or_uuid> --force
```

> **Warning:** This is the most destructive operation in Memex. Consider exporting your vault data before deleting.

## Verification

After deletion, confirm the item is gone:

```bash
# Verify a note was deleted
memex note view <note_uuid>
# Expected: "Note not found"

# Verify a vault was deleted
memex vault list
# The deleted vault should not appear
```

## Recovery

Memex currently implements **hard deletion only**. There is no soft-delete or archival mechanism. To protect against accidental data loss:

- **Back up your PostgreSQL database** before bulk deletions.
- **Export notes** with `memex note view` before deleting.
- **Use `--force` only in scripts** where you have already validated the target UUIDs.

Soft delete (archival with recovery) is planned for a future release.

## See Also

* [Organizing with Vaults](organize-with-vaults.md) — vault lifecycle
* [CLI Commands Reference](../reference/cli-commands.md) — full command documentation
