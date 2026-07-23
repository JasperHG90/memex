eval: surface-node-assets-in-note-metadata

**Definition of Done:** `get_notes_metadata` / `McpNoteMetadata` gains an
`asset_node_ids: list[str]` field listing the page-index sections (by node-hash id)
that contain assets — derived on-read from the already-loaded `page_index['toc']`,
with no new query, column, or migration, and the note-level `has_assets` boolean
untouched.

Scope decisions (locked, mechanical): field name `asset_node_ids`; return type
`sorted list[str]` (a set isn't JSON-serializable); ids returned verbatim from the
stored toc (which are node-hash-space, matching memory-unit `node_ids`).

Scoring policy: deterministic assertions at a hard 100% bar. Rows 1, 2, 4, and 6 are
guardrails — the correctness of the id set, the recursive walk, the read-path (no new
query) invariant, and the id-space compatibility that the downstream inline ticket
depends on.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Asset-bearing sections are listed | A note whose page_index toc has 2 sections with `assets` and 1 without | `asset_node_ids` == exactly the 2 asset-bearing sections' ids; the 3rd is absent | Deterministic: assert `set(asset_node_ids) == {the 2 asset section ids}` | 100% |
| **[GUARDRAIL]** Nested (child) sections are included | A note where an asset-bearing section is a CHILD in the toc hierarchy | That child's id appears in `asset_node_ids` (the walk recurses through `TOCNodeDTO.children`) | Deterministic: assert the nested asset section's id ∈ `asset_node_ids` | 100% |
| Asset-free / no-page-index note yields empty list | (a) a note with a toc but no section assets; (b) a note with no `page_index` | `asset_node_ids == []` in both cases (not null, not missing) | Deterministic: assert `asset_node_ids == []` for both | 100% |
| **[GUARDRAIL]** Read-path only — no new query | `get_notes_metadata([note_id])` | The field is derived from the already-loaded `page_index` blob; no additional nodes-table query is issued vs. today | Deterministic: assert the DB query count is unchanged from the pre-change baseline (spy) | 100% |
| note-level `has_assets` is unchanged | A note with note-scoped attachments | `has_assets == bool(note.assets)` exactly as before; adding `asset_node_ids` does not alter it | Deterministic: assert `has_assets` equals the pre-change value | 100% |
| **[GUARDRAIL]** Returned ids are node-hash-space (intersectable with unit node_ids) | A note whose sections have assets; a memory unit from one of those sections | Every id in `asset_node_ids` is a node-hash-space id equal to a stored toc section id, so `unit.node_ids ∩ asset_node_ids` is well-defined | Deterministic: assert `asset_node_ids ⊆ {toc section ids}` AND type is `list[str]` | 100% |
