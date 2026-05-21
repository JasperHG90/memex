---
name: recall
description: "Search Memex long-term memory. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall — Search Memex

1. **Query source**:
   - If `$ARGUMENTS` is non-empty, use it as the search query.
   - If `$ARGUMENTS` is empty, look for a "Composed query" block in the additional context — the plugin's `UserPromptExpansion` hook supplies one when you invoke `/recall` without arguments. Print a one-line summary of the composed query so the user can see what's being matched.
   - If neither is available, ask the user.

2. **Search strategy**:
   - `memex_memory_search` AND `memex_note_search` in parallel. If insufficient, retry with `expand_query=true`.
   - If still nothing, try `memex_list_entities`. If all fail, say so — do not guess.
   - **Historical queries** ("how has X evolved?", "show me everything/hidden"): use `memex_get_unit_history(unit_id)` or `memex_memory_search(apply_pre_filter=False)`.

3. **Present results**: summarize clearly, include source Note IDs.

4. **Procedure recall**: for "how do I X?" queries, check `procedure:` KV namespace first:
   - `memex_kv_list(namespaces=["procedure"])` → `memex_kv_get(key)` for active value

5. **Memory hygiene**: when asked about stale facts, call `memex_get_lint_flags(vault_id=...)`. Act autonomously on low-risk findings. When a finding has `rule_name='propose_contradiction_winner'`, call `memex_lint_apply_winner` after surfacing the proposal to the user.

6. **Diagnostics**: "how is this vault doing?" → `memex_get_diagnostics_summary(vault_id=...)` — returns cluster count, top entities, and MW score distribution.

## Transcript-aware fallback contract

When you invoke `/recall` with no arguments, a `UserPromptExpansion` hook reads the conversation transcript, extracts the last `MEMEX_CC_RECALL_TURNS` turns (default 3, range 1–10), and injects a composed query as additional context. The composed text is enclosed between `--- Composed query (last N turns) ---` markers so the skill can locate it deterministically. If an explicit query is supplied, the hook does not run — your `$ARGUMENTS` always wins.
