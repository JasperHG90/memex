# Node identifiers: three IDs, three jobs

A note that Memex has indexed with the page-index strategy is broken into
**sections** — one per heading. Each section is a row in the `nodes` table,
and each row carries *three* different identifiers. They look interchangeable
but they are not, and mixing them up is the single most common reason a
"why is this empty?" question lands in the issue tracker.

Here are the three, and what each one is for.

## `node.id` — the internal row key

A random UUID, assigned when the row is written. It identifies the database
row and nothing else. Memex does not hand this ID to agents or surface it in
any tool response, because it is not stable across re-ingest: ingest the same
note twice and the section gets a brand-new `node.id` each time. Treat it as
private plumbing.

## `node.node_hash` — the public section ID

An MD5 hash of the section's text. This is the ID you actually see: it is what
`memex_get_page_indices` returns for each entry in the table of contents, and
it is what `memex_get_nodes` accepts. Because it is derived from content, it is
stable — the same section text always hashes to the same ID — which is exactly
what you want for a public handle.

The hash is 32 hex characters, which happens to be a valid UUID once you strip
the dashes. That is why you can take a page-index ID and pass it straight to
`memex_get_nodes`: the lookup falls back to matching on `node_hash` when the
value isn't a real row key.

## `node.block_id` — the join key to memory units

A foreign key to the `chunks` table. Several sections may be grouped into one
chunk for embedding and retrieval, so this is the coarser, storage-level ID.
Crucially, **this is the ID that memory units join on**: a memory unit records
the `chunk_id` it was extracted from, which is a `block_id`, *not* a
`node_hash`.

This is the gotcha. If you take a section ID from the page index — a
`node_hash` — and pass it to `memex_get_memory_units`, you get an empty list,
silently. The page-index ID lives in a different identifier space from the one
memory units are keyed by. The fix is one hop: read the section with
`memex_get_nodes`, take its `block_id`, and pass *that* to
`memex_get_memory_units`.

## Putting it together

```
memex_get_page_indices      → TOC entries, each with a node_hash id
        │ (node_hash)
        ▼
memex_get_nodes([id])       → NodeDTO, which exposes block_id
        │ (block_id)
        ▼
memex_get_memory_units      → the facts extracted from that section
   (by chunk id)
```

For the step-by-step version, see
[Inspect a note's structure](../how-to/inspect-note-structure.md).
