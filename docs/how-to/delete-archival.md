# Deleting Content

Memex implements a **Cascading Deletion** policy to ensure data consistency.

## Deleting a Note

When you delete a note, Memex cleans up all derived data.

```bash
memex note delete <note_uuid>
```

**Cascade Effect:**
1.  **Note**: The entry in the `notes` table is removed.
2.  **FileStore**: The actual file content and assets are deleted from disk.
3.  **Chunks**: All text chunks associated with the document are deleted.
4.  **Memory Units**: All facts, observations, and opinions derived *solely* from this document are deleted.
5.  **Entity Links**: Links between the deleted units and entities are removed.
    - *Note*: The Entities themselves are **NOT** deleted, as they may be linked to other documents.

## Deleting a Vault

Deleting a vault is a destructive operation that removes **everything** within it.

```bash
memex vault delete <vault_uuid>
```

**Cascade Effect:**
1.  All Documents in the vault.
2.  All Memories in the vault.
3.  All Entities specific to the vault (if scoped).
4.  The Vault definition itself.

## Archival (Soft Delete)

Currently, Memex supports hard deletion. Soft delete (archival) is planned for future versions to allow recovery.
