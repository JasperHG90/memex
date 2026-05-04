---
name: recall
description: "Search Memex long-term memory for relevant information. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall — Search Memex Long-Term Memory

1. **Query**: use `$ARGUMENTS` as the search query. If empty, ask the user.

2. **Search strategy** (two-stage with expansion fallback):
   a. `memex_memory_search` AND `memex_note_search` in parallel (no expansion).
   b. If insufficient, retry both with `expand_query=true`.
   c. If still nothing, try `memex_list_entities`. If all fail, say so — do not guess.

   **Layer routing** (see `.claude/rules/memory-layers.md`): Episodic → `memex_note_search` / `memex_find_note`; Semantic → `memex_memory_search`; Conceptual → `memex_survey`; Procedural → `memex_kv_search` / `memex_kv_get(prefix='procedure:')`.

   **Historical queries** ("how has X evolved?", "show me everything including hidden"): do NOT use resolution flow. Use `memex_get_unit_history(unit_id)` for ordered chains, or `memex_memory_search(apply_pre_filter=False)` for broad audit. Triggers: "evolved", "used to", "history of", "audit", "show me everything/hidden".

3. **Present results**: summarize clearly, include source Note IDs for drill-down.

4. **Procedure recall**: for "how do I X?" queries, check `procedure:` KV namespace first:
   - `memex_kv_list(namespaces=["procedure"])` for matching keys
   - `memex_kv_get(key)` for active value; `memex_kv_get(key, include_history=true)` for version history
   - After using a procedure, close the loop: `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`

5. **Memory hygiene**: when asked about memory state or stale facts, call `memex_get_lint_flags(vault_id=...)`. Surface high-confidence findings; act autonomously on low-risk ones (e.g. `memex_memory_deprioritize` for low-MW units).

6. **Memories due for review**: when asked "what's due?" or "what should I revisit?", call `memex_get_due_for_review(vault_id?)`. After review, call `memex_memory_review(unit_id, quality)` with quality ∈ {again, hard, good, easy}. Five consecutive 'again' ratings auto-deprioritize.

7. **Vault diagnostics**: "how is this vault doing?" → `memex_get_diagnostics_summary(vault_id=...)`. For full output, point to `memex diagnostics manifold|retrieval|summary --vault X`.
