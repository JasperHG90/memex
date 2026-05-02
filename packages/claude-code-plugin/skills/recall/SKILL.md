---
name: recall
description: "Search Memex long-term memory for relevant information. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall — Search Memex Long-Term Memory

You have been invoked via the `/recall` slash command.

## Instructions

1. **Determine the search query.**
   - Use `$ARGUMENTS` as the search query.
   - If `$ARGUMENTS` is empty, ask the user what they would like to recall.

2. **Search strategy** (two-stage with expansion fallback):
   a. **First pass**: call `memex_memory_search` AND `memex_note_search` in parallel (no expansion).
   b. **If insufficient**: retry both with `expand_query=true` for broader recall via LLM query expansion.
   c. If still no results, call `memex_list_entities` to browse the knowledge graph.
   d. If nothing is found after all three strategies, say so — do not guess.

3. **Present results.**
   - Summarize the findings in a clear, readable format.
   - Include source Note IDs so the user can drill deeper with `memex_read_note`.
   - If no results are found, tell the user and suggest alternative queries.

<!--
Tier A — /recall verb extensions
F8:  WS-linter      (get_lint_flags surfacing)
F20: WS-revisit     (get_due_for_review surfacing)
F32: WS-diagnostics (get_diagnostics_summary surfacing)

# --- F8 ---  (filled by WS-linter)

# --- F20 --- (filled by WS-revisit)

# --- F32 --- (filled by WS-diagnostics)
4. **Vault diagnostics** (when the user asks "how is this vault doing?",
   "what's in vault X?", "check vault health", or wants visualisation):
   call `memex_get_diagnostics_summary(vault_id=...)` to surface unit
   counts by status (active / stale / deprioritized), pending lint counts
   by type, cluster_count (null when the UMAP manifold cache is cold),
   avg MW score, and top retrieved entities. For full JSON or visual
   plots, point the user to the CLI: `memex diagnostics manifold |
   retrieval | summary --vault X`.
-->
