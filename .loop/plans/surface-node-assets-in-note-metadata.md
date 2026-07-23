# surface-node-assets-in-note-metadata

## 1. Title

Enrich the note-metadata payload (`get_notes_metadata` / `McpNoteMetadata`)
with a per-note set of asset-bearing page-index node ids, so a consumer can
tell which section owns an embedded image, not just that the note has one
somewhere.

## 2. Size / Effort

**S.** One derivation helper plus one field threaded through two existing
shapes (`get_notes_metadata` dict, `McpNoteMetadata`). No new query, no
migration, no ingest/extraction change. Effort is dominated by the recursive
toc walk and its tests, not by surface area.

## 3. Triggered by

Operator request this session: note metadata should become node-asset-aware
as standalone value, and as the prerequisite the separate
`memory-search-inline-metadata` ticket will consume to derive a per-unit
`node_has_assets`. The consumption is explicitly out of scope here (see §5).

## 4. Context

`get_notes_metadata` at
`packages/core/src/memex_core/services/notes.py:714` fetches whole `Note`
rows in one query (`packages/core/src/memex_core/services/notes.py:727`),
pulling the full `Note.page_index` JSONB
(`packages/core/src/memex_core/memory/sql_models.py:284`) into memory. That
JSONB carries both a `metadata` envelope and a `toc` thin tree. Today the
loop at `packages/core/src/memex_core/services/notes.py:742` reads only
`page_index['metadata']` and discards `page_index['toc']`.

Each stored toc entry is a `TOCNodeDTO`
(`packages/common/src/memex_common/schemas.py:1438`) whose `assets` field
(`packages/common/src/memex_common/schemas.py:1447`) is a list of
`SectionAssetDTO` (`packages/common/src/memex_common/schemas.py:1115`,
`{path, alt_text, filename}`), populated for image-bearing sections by
migration `056_node_assets`
(`packages/core/src/memex_core/alembic/versions/056_node_assets.py`). The
toc is recursive: `TOCNodeDTO.children`
(`packages/common/src/memex_common/schemas.py:1448`) nests arbitrarily deep.

The data needed is therefore already materialized and already in memory on
every `get_notes_metadata` call. What is missing: the service never walks the
toc to report which node ids carry assets. The MCP shape `McpNoteMetadata`
(`packages/mcp/src/memex_mcp/models.py:272`) likewise exposes only the
note-level `has_assets` boolean and no per-node breakdown; the server builds
it from the returned dict at
`packages/mcp/src/memex_mcp/server.py:2374`.

The stored toc `id` is NOT the random `TOCNode.id` used during extraction
(`packages/core/src/memex_core/memory/extraction/models.py:147`,
`str(uuid.uuid4())`). Before storage, `build_thin_tree`
(`packages/core/src/memex_core/memory/extraction/pipeline/diffing.py:230`)
rewrites every toc `id` to the section content hash via `collect_toc_hashes`
(`packages/core/src/memex_core/memory/extraction/pipeline/diffing.py:185`)
and `replace_tree_ids`
(`packages/core/src/memex_core/memory/extraction/pipeline/diffing.py:211`).
That content hash is the DB `node_hash`
(`packages/common/src/memex_common/schemas.py:1133`), which is the id space
`get_nodes` already resolves against
(`packages/core/src/memex_core/services/notes.py:709`) and which memory-unit
`node_ids` are documented to use ("Page-index node IDs linked to the source
chunk", `packages/common/src/memex_common/schemas.py:566`). See §11 for the
one residual verification the implementer must lock before shipping.

## 5. Non-goals / out of scope

- Do NOT scope the `memory_search` inline consumption that derives a
  per-unit `node_has_assets` — that is the separate
  `memory-search-inline-metadata` ticket.
- Do NOT change the ingest/extraction/page_index build, do NOT add a DB
  column, and do NOT add a migration. The derivation is computed on read
  from the already-loaded toc.
- Do NOT change `get_nodes` or `McpNode`
  (`packages/mcp/src/memex_mcp/models.py:283`), and do NOT change the
  note-level `has_assets` boolean at
  `packages/core/src/memex_core/services/notes.py:746`. Both stay exactly
  as-is.
- Do NOT surface full asset refs (`{path, alt_text, filename}`) in note
  metadata. Node ids only; the full refs stay reachable via
  `get_nodes` / `McpNode.assets`
  (`packages/mcp/src/memex_mcp/models.py:290`).

## 6. Requirements & restrictions

Operator-locked this session (record as requirements, not open forks):

- **R1. No new query, no migration.** Derive from the toc already loaded by
  the existing `SELECT Note` at
  `packages/core/src/memex_core/services/notes.py:727`. Stop discarding
  `page_index['toc']`.
- **R2. Computed-on-read.** Derive the asset-bearing node id set from the
  in-memory toc on each `get_notes_metadata` call. No materialized summary
  column. Materializing a redundant summary is explicitly rejected.
- **R3. Shape = identifiers only.** Add a set/list of node ids (the toc
  entry ids, i.e. the `node_hash` id space) to the dict
  `get_notes_metadata` returns and to `McpNoteMetadata`. Not the full asset
  refs. Keep the payload lean.
- **R4. Recurse the toc.** The walk MUST descend `TOCNodeDTO.children`
  (`packages/common/src/memex_common/schemas.py:1448`) and collect
  asset-bearing ids at every level, not just roots.
- **R5. Empty default.** For notes skipped at
  `packages/core/src/memex_core/services/notes.py:740` (no `page_index`) or
  `packages/core/src/memex_core/services/notes.py:743` (no `metadata`), and
  for notes whose toc is present but carries no assets, the new field is an
  empty collection. `McpNoteMetadata` must default the field to empty so
  existing construction sites and callers that never set it keep working.

Repo principles this change must respect, each cited to where the repo
states it:

- **Every change ships a test; bug/behaviour proven by a test that runs in
  the gate** (`.claude/rules/python-testing.md`, constraint
  `all-code-needs-tests`). See §8.
- **Surgical changes** — touch only the metadata derivation path; do not
  refactor the surrounding loop or adjacent extraction code
  (`CLAUDE.md` section 3, "Surgical Changes").
- **Simplicity first** — a trivial recursive walk, no configurability or
  speculative generality (`CLAUDE.md` section 2).
- **Tests are real code, linted and type-checked; no `# type: ignore`,
  `skip`, or `xfail` to green a gate** (`.claude/rules/python-testing.md`,
  constraint `tests-are-real-code`).
- **Fix, never silence, any pre-existing failure a gate surfaces**
  (`.claude/rules/pre-existing-issues.md`).
- **Run the adversarial review before declaring done**
  (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

- `packages/core/src/memex_core/services/notes.py:742` — where the loop
  reads `page_index['metadata']`. Add: after reading metadata, walk
  `doc.page_index.get('toc', [])` recursively and set the new key (e.g.
  `metadata['asset_node_ids']`) to the sorted list of ids whose toc entry
  has a non-empty `assets`. Leave the `has_assets` line at
  `packages/core/src/memex_core/services/notes.py:746` untouched. Prefer a
  small module-level pure helper (e.g. `collect_asset_node_ids(page_index)
  -> list[str]`) so the walk is unit-testable offline without a DB — this is
  the function the root gate test targets (§8).
- `packages/mcp/src/memex_mcp/models.py:272` — add the field to
  `McpNoteMetadata` (recommended `asset_node_ids: list[str] = []`, matching
  the `list[str]` id space of `node_ids` at
  `packages/common/src/memex_common/schemas.py:566`), defaulted empty per
  R5.
- `packages/mcp/src/memex_mcp/server.py:2374` — thread the new key from the
  returned dict into the `McpNoteMetadata(...)` construction
  (`asset_node_ids=meta.get('asset_node_ids', [])`), preserving the
  empty-default fallback used by the sibling fields.

Test homes (each test named in §8 has its file listed here):

- `tests/test_note_asset_node_ids.py` (NEW, root `./tests/`, offline, not
  `integration`-marked) — the loop-gating behaviour test over the pure
  helper.
- `packages/core/tests/integration/test_int_get_notes_metadata.py:1` (existing,
  `@pytest.mark.integration`) — add a service-level case; mirrors the
  offline test through the real DB path. Runs only manually.
- `packages/core/tests/unit/test_note_relations_unit.py:204` (existing,
  where `McpNoteMetadata` is constructed) — add a shape assertion that the
  new field defaults empty and round-trips. Runs only manually under the
  package dir.

## 8. Tests & validation gates

**Eval marker (acceptance layer):** `.loop/evals/surface-node-assets-in-note-metadata.md`
— 6 deterministic scenarios at 100%. Guardrails: correct asset id set; recursive walk
of nested sections; read-path-only (no new query); ids are node-hash-space
(intersectable with unit `node_ids`, which the inline ticket depends on).

Gates (verified this session):

- **`just test`** = `uv run pytest tests`
  (`justfile:65`), which collects ONLY the root `./tests/` tree (~56 tests).
  `packages/core/tests` and `packages/mcp/tests` are NOT collected by this
  loop gate. `addopts` excludes `integration`-marked tests by default
  (`pyproject.toml:78`, `-m 'not integration'`). Therefore the loop-gating
  behaviour test MUST live in root `./tests/`, be offline, and carry no
  `integration` marker.
- **`just prek`** = `uv run prek run -a` (`justfile:61`) = ruff + mypy +
  format.

Tests to add:

- **Loop-gating behaviour test** — `tests/test_note_asset_node_ids.py`,
  offline, exercising the pure helper `collect_asset_node_ids` against
  hand-built `page_index` dicts (the same dict shape the existing
  integration test constructs at
  `packages/core/tests/integration/test_int_get_notes_metadata.py`). It MUST
  assert:
  1. The returned collection contains exactly the ids of toc entries whose
     `assets` is non-empty, INCLUDING assets on nested `children` at depth
     > 1 (R4).
  2. A note whose toc has no assets yields an empty collection; a note with
     no `page_index` / no `metadata` yields an empty collection (R5).
  3. The note-level `has_assets` boolean is unchanged by this ticket (assert
     it stays derived from `doc.assets`, independent of the new field).
  Cover the cases with `@pytest.mark.parametrize`
  (`.claude/rules/python-testing.md`, "Writing good tests").
- **Service mirror (manual)** — a case in
  `packages/core/tests/integration/test_int_get_notes_metadata.py` seeding a
  note whose stored toc carries assets on a nested section, asserting
  `get_notes_metadata` returns the matching `asset_node_ids`. Uses the real
  metastore fixtures already in that file; runs under `-m integration`.
- **Shape round-trip (manual)** — extend
  `packages/core/tests/unit/test_note_relations_unit.py` to assert
  `McpNoteMetadata` defaults `asset_node_ids` to empty and accepts a
  populated list.

Both `just test` and `just prek` must pass before done. Run the adversarial
review (`.claude/rules/adversarial-reviews.md`) after gates are green.

`require_eval: true` (`.loop/config.json`): the loop refuses pickup until an
eval marker exists. Author it separately with the `create-eval` skill (see
final note).

## 9. Risk assessment

- **Blast radius: narrow.** One read-path derivation plus one additive,
  empty-defaulted field on two shapes. `get_notes_metadata` feeds MCP
  `memex_get_notes_metadata` and the hermes plugin (whose tests mock the API
  and assert call shape, e.g. `packages/hermes-plugin/tests/test_tools.py`);
  an additive dict key does not break those.
- **Reversibility: high.** Removing the field and the helper reverts
  cleanly; no data written, no schema touched.
- **Likeliest failure modes:**
  1. **Wrong id space** — returning the random extraction-time
     `TOCNode.id` instead of the stored content-hash id. The stored toc has
     already been id-rewritten (`build_thin_tree`), so reading
     `entry['id']` off `doc.page_index['toc']` yields the correct
     (`node_hash`) space. Do NOT re-derive ids. See §11.
  2. **Non-recursive walk** missing nested assets (R4) — guarded by test
     case 1.
  3. **KeyError / type assumptions** on malformed toc entries — the walk
     must tolerate `assets` absent or `children` absent, mirroring the
     existing `None`/`isinstance` defensiveness at
     `packages/core/src/memex_core/services/notes.py:740`.

## 10. Subtickets

1. Add the pure helper `collect_asset_node_ids(page_index) -> list[str]`
   near `get_notes_metadata` and write the offline gate test
   (`tests/test_note_asset_node_ids.py`). Verify: `just test` green.
2. Wire the helper into `get_notes_metadata` at
   `packages/core/src/memex_core/services/notes.py:742`, setting
   `metadata['asset_node_ids']`. Verify: add the manual integration case.
3. Add `asset_node_ids` to `McpNoteMetadata`
   (`packages/mcp/src/memex_mcp/models.py:272`) and thread it through
   `packages/mcp/src/memex_mcp/server.py:2374`; extend the shape test.
   Verify: `just prek` (mypy) green.

Order is dependency-driven: helper and its gate test first (proves the
derivation in the collected suite), then the two thread-through edits.

## 11. Open questions

- **Q1 (the one real correctness risk): node-id identity across surfaces.**
  Investigation this session found the stored `page_index['toc']` entry `id`
  is rewritten to the section content hash (`node_hash`) by `build_thin_tree`
  / `replace_tree_ids`
  (`packages/core/src/memex_core/memory/extraction/pipeline/diffing.py:230`,
  `:211`), which is the same id space `get_nodes` resolves
  (`packages/core/src/memex_core/services/notes.py:709`) and that memory-unit
  `node_ids` are documented to carry
  (`packages/common/src/memex_common/schemas.py:566`). **Recommendation:**
  return the stored toc `id` values verbatim (no re-hashing, no UUID
  conversion) so the set lives in the `node_hash` space downstream
  `memory-search-inline-metadata` will intersect against. Before shipping,
  the implementer should confirm with one seeded end-to-end note that a
  memory unit's `node_ids` value for an asset-bearing section string-equals
  the id this ticket returns for that section — the integration mirror test
  (§8) is the natural place to lock it. If they diverge in practice,
  surface it rather than papering over with a translation layer.
- **Q2: field name.** **Recommendation:** `asset_node_ids` (explicit, plural,
  mirrors the existing `node_ids: list[str]` field at
  `packages/common/src/memex_common/schemas.py:566`). Adopt unless the
  operator prefers another name.
- **Q3: return type — set vs sorted list.** The dict/JSON payload and
  `McpNoteMetadata` cannot carry a Python `set` (not JSON-serializable and
  order-unstable for snapshot/equality tests). **Recommendation:** derive as
  a `set` for dedup, return a **sorted `list[str]`** for deterministic
  output and stable tests. Operator to confirm the list default is
  acceptable.
