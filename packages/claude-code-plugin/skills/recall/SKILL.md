---
name: recall
description: "Search Memex long-term memory. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall — Search Memex

1. Use `$ARGUMENTS` as the search query. If empty, ask the user.

2. **Search strategy**:
   - `memex_memory_search` AND `memex_note_search` in parallel. If insufficient, retry with `expand_query=true`.
   - If still nothing, try `memex_list_entities`. If all fail, say so — do not guess.
   - **Historical queries** ("how has X evolved?", "show me everything/hidden"): use `memex_get_unit_history(unit_id)` or `memex_memory_search(apply_pre_filter=False)`.

3. **Present results**: summarize clearly, include source Note IDs.

4. **Procedure recall**: for "how do I X?" queries, check `procedure:` KV namespace first:
   - `memex_kv_list(namespaces=["procedure"])` → `memex_kv_get(key)` for active value

5. **Memory hygiene**: when asked about stale facts, call `memex_get_lint_flags(vault_id=...)`. Act autonomously on low-risk findings. When a finding has `rule_name='propose_contradiction_winner'`, call `memex_lint_apply_winner` after surfacing the proposal to the user.

6. **Diagnostics**: "how is this vault doing?" → `memex_get_diagnostics_summary(vault_id=...)`.
