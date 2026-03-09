
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

<constraint name="proactive-memory-capture" priority="critical">
### Capture — MANDATORY

Call `memex_add_note` (with `background: true`, `author: "claude-code"`) when any of these apply:

1. Completed a multi-step task (save what was done, decisions, outcome)
2. Diagnosed a bug root cause (save symptom, cause, fix)
3. Made/discovered an architectural decision (save decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

**Keep notes concise** (hard maximum: 300 tokens). Capture the key insight, not a detailed report. No per-file changelogs.
</constraint>

### Retrieval

Session start context is automatic via hook. Do NOT redundantly search at session start.

PROHIBITED:
- `memex_list_notes` for discovery.
- Fabricating Note/Node/Unit IDs. Only use IDs from tool output.
- `memex_get_note_metadata` after `memex_note_search` (metadata already inline).
- `memex_read_note` on notes over 500 tokens. Use `memex_get_page_index` + `memex_get_node`.
- Creating diagrams/charts without first checking assets for visual context via `memex_list_assets` → `memex_get_resource`.

**Search** — pick by query type, or run both in parallel:
- `memex_memory_search` — atomic facts, observations, mental models. Broad queries.
- `memex_note_search` — ranked source notes with inline metadata. Targeted lookup.

**Filter** — before reading:
- After `memex_memory_search`: call `memex_get_note_metadata` to check relevance.
- After `memex_note_search`: use inline metadata directly.

**Read** — only confirmed-relevant notes:
1. `memex_get_page_index` → TOC + node IDs
2. `memex_get_node` (parallel) → section content
3. `memex_read_note` → only when total_tokens < 500

**Assets** — required when `has_assets: true`:
- `memex_list_assets` → `memex_get_resource` → render inline.

### Citations

When presenting information from Memex, use numbered citations [1], [2] etc. inline. Add a reference list at the end with the source type prefix:
- `[note]` — title + note ID
- `[memory]` — title + memory ID + source note ID
- `[asset]` — filename + note ID

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
