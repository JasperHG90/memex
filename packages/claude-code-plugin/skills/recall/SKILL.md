---
name: recall
description: "Search Memex long-term memory for relevant information. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall -- Search Memex Long-Term Memory

You have been invoked via the `/recall` slash command.

## Memex API calls

All Memex operations use the plugin's `mx` helper. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <command> '<json_args>'
```

## Instructions

### Step 1. Parse the search query.

- Use `$ARGUMENTS` as the search query.
- If `$ARGUMENTS` is empty, ask the user what they would like to recall.

### Step 2. Classify query intent and select search strategy.

Determine the best strategy based on the query shape:

| Query Type                                        | Strategy                              |
| :------------------------------------------------ | :------------------------------------ |
| Keyword-heavy / exact match (error msgs, fn names)| `"strategies": ["keyword"]`           |
| Conceptual / exploratory                          | `"strategies": ["semantic", "graph"]` |
| Temporal ("what happened in January?")            | `"strategies": ["temporal"]`          |
| Unclear / broad                                   | Omit `strategies` (all + RRF fusion)  |

### Step 3. Run parallel broad search.

Execute both searches in parallel:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" memory-search '{"query":"<QUERY>","limit":10,"strategies":[<STRATEGIES>]}'
"${CLAUDE_PLUGIN_ROOT}/bin/mx" note-search '{"query":"<QUERY>","limit":10,"strategies":[<STRATEGIES>]}'
```

If the strategy is omitted (broad recall), drop the `strategies` key entirely.

### Step 4. Filter and get metadata.

- **After memory-search**: collect all referenced note IDs, then fetch metadata:
  ```bash
  "${CLAUDE_PLUGIN_ROOT}/bin/mx" get-notes-metadata '{"note_ids":["<ID1>","<ID2>"]}'
  ```
- **After note-search**: metadata is returned inline -- skip this call.

Rank notes by relevance across both result sets. Deduplicate by note ID.

### Step 5. Deep-read top-ranked notes.

For the top-ranked notes:

a. Get the table of contents:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-page-indices "<NOTE_ID>"
```

b. Review the TOC and read the most relevant sections:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-nodes '{"node_ids":["<NODE_ID_1>","<NODE_ID_2>"]}'
```

c. If the page index shows `has_assets: true`:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-assets "<NOTE_ID>"
```
Then read relevant assets as needed.

### Step 6. Present results with citations (MANDATORY).

Summarize the findings in a clear, readable format. Every claim sourced from
Memex MUST have an inline [N] citation.

End the response with a numbered reference list. Each entry uses a type prefix:
- `[note]` title + note ID
- `[memory]` title + memory ID + source note ID
- `[asset]` filename + note ID

Example:
```
[1] [note] Database Migration Guide -- 2eb202ed-bee6-7b2a-f0b9-917e8d5dd6f0
[2] [memory] pgvector requires PostgreSQL 15+ -- abc123 (source: def456)
```

If no results are found across both searches, tell the user and suggest
alternative queries or broader terms.
