## Memex retrieval routing

Route by query type:

- **Title known** → `memex_find_note(query="fragment")` → read via `memex_get_page_indices` + `memex_get_nodes`
- **Relationships / connections** → `memex_list_entities` → `memex_get_entity_cooccurrences` → `memex_get_entity_mentions` → read source notes as needed
- **Content / document lookup** → `memex_memory_search` (broad) and/or `memex_note_search` (targeted) in parallel → `memex_get_notes_metadata` after memory_search (skip after note_search — metadata inline) → read via `memex_get_page_indices` + `memex_get_nodes` (use `memex_read_note` only when total_tokens < 500)
- **Broad** → run entity exploration AND search in parallel
- **Assets** → when `has_assets: true`: `memex_list_assets` → `memex_get_resources`. NEVER skip.

Session start context is automatic. Do NOT redundantly search at session start.

## Memex capture — MANDATORY

Call `memex_add_note` (background: true, author: "claude-code", max 300 tokens) when:

1. Completed a multi-step task (what was done, decisions, outcome)
2. Diagnosed a bug root cause (symptom, cause, fix)
3. Made/discovered an architectural decision (decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

## Memex KV store

- `memex_kv_write(value, key)` / `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()`
- Keys MUST use namespace prefix: `global:`, `user:`, `project:<id>:`, or `app:<id>:`
- Proactively store user preferences and conventions via `memex_kv_write`
- Deletion is user-only — do NOT delete KV entries

## Memex citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim
2. Reference list with type prefix: `[note]` title + note ID, `[memory]` title + memory ID + source note ID, `[asset]` filename + note ID

## Memex prohibitions

- NEVER use `memex_recent_notes` for discovery
- NEVER fabricate Note/Node/Unit IDs — only use IDs from tool output
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata already inline)
- NEVER use `memex_read_note` on notes over 500 tokens — use page_indices + get_nodes
- NEVER create diagrams without first checking assets
- NEVER present Memex data without citations
