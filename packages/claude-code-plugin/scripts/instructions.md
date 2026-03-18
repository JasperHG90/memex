## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

### Capture — MANDATORY

Call `memex_add_note` (with `background: true`, `author: "claude-code"`) when any of these apply:

1. Completed a multi-step task (save what was done, decisions, outcome)
2. Diagnosed a bug root cause (save symptom, cause, fix)
3. Made/discovered an architectural decision (save decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

**Keep notes concise** (hard maximum: 300 tokens). Capture the key insight, not a detailed report. No per-file changelogs.

### Retrieval

Session start context is automatic via hook. Do NOT redundantly search at session start.

Route by query type:

IF you know (or roughly know) a note title:
- `memex_find_note(query="title fragment")` → note IDs, titles, similarity scores
- Then read via `memex_get_page_indices` → `memex_get_nodes` as needed

IF query asks about relationships, connections, "how X relates to Y", or landscape:
- `memex_list_entities(query="X")` → entity IDs, types, mention counts
- `memex_get_entity_cooccurrences(entity_id)` → related entities with names, types, counts
- `memex_get_entity_mentions(entity_id)` → source facts linking back to notes
- Then read source notes via Search/Read below as needed

IF query asks about specific content or document lookup:
- **Search**: `memex_memory_search` (broad) and/or `memex_note_search` (targeted). Run in parallel.
- **Filter**: after `memex_memory_search`, call `memex_get_notes_metadata` with Note IDs. After `memex_note_search`, metadata is inline — skip.
- **Read**: `memex_get_page_indices` → `memex_get_nodes` (batch). `memex_read_note` only when total_tokens < 500.
- **Assets**: IF `has_assets: true` in page_index/metadata → `memex_list_assets` → `memex_get_resources` for each. Use images as visual input. Reproduce diagrams as Mermaid/ASCII in response. NEVER skip this step.

IF query is broad: run entity exploration AND search in parallel.

IF storing/retrieving structured facts, preferences, or conventions:
- `memex_kv_write(value, key, vault_id)` — store a user fact or preference
- `memex_kv_get(key)` — exact key lookup
- `memex_kv_search(query)` — fuzzy semantic search over stored facts
- `memex_kv_list()` — list all stored facts
- When the user states a preference, convention, or static fact, proactively store it via `memex_kv_write`.
- Deletion is user-only (CLI `memex kv delete`). Do NOT attempt to delete KV entries.

All vault parameters on MCP tools are **optional** — they default to resolved config values.

PROHIBITED:
- `memex_recent_notes` for discovery.
- Fabricating Note/Node/Unit IDs. Only use IDs from tool output.
- `memex_get_notes_metadata` after `memex_note_search` (metadata already inline).
- `memex_read_note` on notes over 500 tokens. Use `memex_get_page_indices` + `memex_get_nodes`.
- Creating diagrams without first checking assets via `memex_list_assets` → `memex_get_resources`.
- Presenting Memex information without citations.

### Citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim from Memex.
2. Reference list at end of response. Each entry uses a type prefix:
   - `[note]` — title + note ID
   - `[memory]` — title + memory ID + source note ID
   - `[asset]` — filename + note ID

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
