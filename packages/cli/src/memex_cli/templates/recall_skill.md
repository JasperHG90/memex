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

2. **Search strategy** (execute in order, stop when you have useful results):
   a. Call `memex_search` with the query to retrieve atomic facts and memory units.
   b. If the results are insufficient, call `memex_note_search` to search source documents.
   c. If keyword-based search yields nothing, call `memex_list_entities` to browse
      the knowledge graph for relevant entities.

3. **Present results.**
   - Summarize the findings in a clear, readable format.
   - Include source Note IDs so the user can drill deeper with `memex_read_note`.
   - If no results are found, tell the user and suggest alternative queries.
