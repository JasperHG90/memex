## When the user reports an issue resolved (5-step flow)

1. **Disambiguate** — if scope is ambiguous (multiple notes, no temporal anchor, conflated issues), ASK before writing.

2. **Route by info quality**, then pick a coverage path:
   - Title known → `memex_find_note(query="...")`
   - Content only → `memex_memory_search`
   - Then pick **one** path:
     - **A: entity-anchored** (highest recall when topic ↔ entity): `memex_list_entities` → `memex_get_entity_mentions`
     - **B: cross-note semantic** (no entity anchor): `memex_memory_search(query="...", top_k=30)`. **`top_k` must be ≥30**.
     - **C: single-note PageIndex** (provably one note): `memex_get_page_indices` → `memex_get_memory_units(chunk_ids=[...])`

3. **LLM-judge the candidates** — READ the unit bodies and pick the fix-relevant subset. NEVER bulk-write against the raw candidate set.

4. **+5. Paired writes** against the LLM-judged-relevant subset only:
   ```
   memex_record_outcome(unit_ids=[...], success=false, reason="user confirmed fixed")
   memex_memory_deprioritize(unit_id=..., reason="user confirmed fixed YYYY-MM-DD")
   ```
   Both against the **same** subset. The two verbs are orthogonal:
   - `record_outcome(success=...)` — MW gradient, append-only, not reversible
   - `memory_deprioritize(reason)` — binary surface state, reversible via `memory_restore`

**Imperfect recall is by design** — exploration is the safety net. Units that slip past will re-surface; another `record_outcome(success=false)` compounds the MW penalty.

## Historical / audit-query routing (NOT the resolution flow)

Triggers: "evolved", "used to", "history of", "what changed", "audit", "show me everything/hidden".

- **Specific unit timeline** → `memex_get_unit_history(unit_id)` — contradiction links, oldest → newest
- **Broad audit** → `memex_memory_search(query="...", apply_pre_filter=False)` — bypasses MW/FSFM/confidence filters; post-reranker boosts still apply

"X is resolved" → resolution flow (steps 1–5). "How has my view on X changed" → historical routing.

## What is NOT a gap (do NOT request)

- A combined `memex_resolve()` endpoint — compose `record_outcome` + `deprioritize` at the agent layer
- A `resolved_at` column — maintenance ledger already records when deprioritize fired
- A `resolution_type` enum on deprioritize — free-text `reason` carries the same info
- A `bulk-by-source` parameter — agents iterate
- Note-level deprioritize — notes are episodic anchors; deprioritize applies to derived units
