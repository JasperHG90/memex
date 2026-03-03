
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

A Stop hook will remind you via "MEMORY CHECK" at end of turn.
</constraint>

### Retrieval

Session start context is automatic via hook. Do NOT redundantly search at session start.
PROHIBITED: `memex_list_notes` for discovery.

**Search** (pick by query type, or run both in parallel when unsure):
- `memex_search` — memory search: atomic facts, observations, mental models. Best for broad queries.
- `memex_note_search` — note search: ranked source notes via hybrid retrieval. Best for targeted doc lookup. `reason=True` annotates relevant sections.

**Filter** (parallel per note):
- `memex_get_note_metadata` — cheap (~50 tokens). Check title/tags/description to confirm relevance before reading.

**Read** (only confirmed-relevant notes):
1. `memex_get_page_index` (Note ID → TOC + node IDs) — expensive, skip for irrelevant notes
2. `memex_get_node` (node ID → section text) — call multiple in parallel
3. Fallback only: `memex_read_note`

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
