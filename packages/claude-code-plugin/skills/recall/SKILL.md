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
