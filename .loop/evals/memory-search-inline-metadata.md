eval: memory-search-inline-metadata

**Definition of Done:** an opt-in `include_metadata` flag on `memory_search` inlines
two lean fields per unit — `note_total_tokens` (note-level; gates read-vs-paginate) and
`node_has_assets` (node-level; whether this unit's section has an image) — with the
default path byte-identical to today and no new query on either field.

Scope decision (operator, 2026-07-23): **two-tier**, `note_total_tokens: int | None` +
`node_has_assets: bool`. `note_total_tokens` is note-scoped (same value across units of
a note); `node_has_assets` is per-unit (`bool(unit.node_ids ∩ note.asset_node_ids)`).
Both ride the `get_notes_metadata` fetch already made (`server.py:1793`). All other
`McpNoteMetadata` fields omitted (redundant or token cost). **Depends on**
`surface-node-assets-in-note-metadata` (provides `asset_node_ids`).

Scoring policy: deterministic assertions on the tool response at a hard 100% bar.
Rows 1 and 3 are guardrails (default payload unchanged; no field leak) and must pass 100%.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Default output is unchanged | `memory_search("query")` with the flag omitted (default `False`) | Response is byte-identical to the pre-change output; neither `note_total_tokens` nor `node_has_assets` present on any unit | Deterministic: assert serialized result equals the pre-change baseline AND neither field key in any unit | 100% |
| Flag=true inlines the note's total_tokens | `memory_search("query", include_metadata=True)` over a vault with known notes | Each result carries `note_total_tokens` equal to the parent note's `total_tokens` (the same value `get_notes_metadata` would return) | Deterministic: assert `unit.note_total_tokens == parent_note.total_tokens` for each result | 100% |
| **[GUARDRAIL]** No `null` field leaks when flag is false | `memory_search("query")` (flag false), inspect raw serialized payload | The `note_total_tokens` key is ABSENT, not present-with-`null` | Deterministic: assert `'note_total_tokens' not in serialized_unit` | 100% |
| No extra retrieval round trip is introduced | `memory_search("query", include_metadata=True)` | Value is populated from the `get_notes_metadata` fetch the tool already performs (`server.py:1793`) — no new call added | Deterministic: assert value present AND (call-count spy) the note-metadata fetch happens at most once | 100% |
| `note_total_tokens` is note-scoped, shared across units from the same note | `memory_search("query", include_metadata=True)` where ≥2 returned units come from the SAME note | Both units carry the identical `note_total_tokens` (the parent note's size), not each unit's own text length | Deterministic: assert the two units' `note_total_tokens` are equal AND equal the parent note's size | 100% |
| `node_has_assets` is true when the unit's section owns an asset | Flag=true; a unit whose `node_ids` include a section listed in the note's `asset_node_ids` | `node_has_assets == True` for that unit | Deterministic: assert `unit.node_has_assets is True` AND `set(unit.node_ids) & set(note.asset_node_ids)` is non-empty | 100% |
| `node_has_assets` is false when the unit's section owns no asset | Flag=true; a unit whose `node_ids` intersect none of the note's `asset_node_ids` | `node_has_assets == False` | Deterministic: assert `unit.node_has_assets is False` AND the intersection is empty | 100% |
| **[GUARDRAIL]** `node_has_assets` is per-unit, not note-scoped | Flag=true; two units from the SAME note, one from an asset-bearing section and one not | The two units carry DIFFERENT `node_has_assets` (True vs False) even though `note_total_tokens` is identical | Deterministic: assert the two units' `node_has_assets` differ AND their `note_total_tokens` match | 100% |
