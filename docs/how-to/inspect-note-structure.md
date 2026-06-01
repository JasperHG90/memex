# Inspect a note's structure

You have a note's ID and you want to drill into it: see its sections, read one,
pull the facts that section produced, or find the images it embeds — without
dragging the whole note into context. This guide walks the read path.

It assumes the note was indexed with the page-index strategy (the default for
documents with headings).

## Walk the table of contents

Start with the page index. It returns one entry per section, with titles,
token counts, and section IDs — but not the section text.

```python
page_index = await api.get_note_page_index(note_id)
for node in page_index["toc"]:
    print(node["id"], node["title"], node["subtree_tokens"])
```

Over MCP this is `memex_get_page_indices`. Use the `subtree_tokens` on each
entry to decide what is worth reading before you spend the budget.

## Read a section

Take a leaf section's ID from the TOC and read its content:

```python
nodes = await api.get_nodes([section_id])
section = nodes[0]
print(section.text)
```

Over MCP this is `memex_get_nodes`. Each returned node carries a `block_id` and
an `assets` list — both matter below.

## Pull the facts a section produced

Memory units join on the *chunk* ID, not the section ID. The section's
`block_id` is that chunk ID, so pivot through it:

```python
block_id = section.block_id
units = await api.get_memory_units_by_chunks([block_id], vault_id=note.vault_id)
```

Passing the page-index section ID directly to `get_memory_units` returns an
empty list — see [Node identifiers](../explanation/identifiers.md) for why.
The `block_id` is the bridge.

## See which images a section embeds

Embedded image references are parsed at ingest and attached per section, so you
can see them without fetching any files. Each section node carries an `assets`
list:

```python
for asset in section.assets:
    print(asset.alt_text, "→", asset.path)
```

The same `assets` array rides along on every page-index TOC entry, so you can
spot which sections own images straight from `get_page_indices` — no extra
call. Each entry has `path`, `alt_text` (the markdown `![alt](…)` text, or
`null`), and `filename`. To retrieve the file itself, feed `path` to
`memex_get_resources`.

This covers `![alt](path)` markdown, Obsidian `![[wiki]]` embeds, and inline
HTML `<img>` tags. Remote `http(s)://` images are skipped — they are not
Memex-stored assets.

## Backfill assets for older notes

Notes ingested before per-section assets existed have an empty `assets` list.
Populate them in place:

```bash
memex database backfill-section-assets --vault my-vault
```

The command re-parses each section's stored text. It is safe to re-run — it
only touches sections whose `assets` is still empty.
