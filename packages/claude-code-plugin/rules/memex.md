## Memex retrieval routing

Route by query type:

- **Title known** → `memex_find_note(query="fragment")` → read via `memex_get_page_indices` + `memex_get_nodes`
- **Relationships / connections** → `memex_list_entities` → `memex_get_entity_cooccurrences` → `memex_get_entity_mentions` → read source notes as needed
- **Content / document lookup** → Two-stage retrieval:
  1. **First pass**: run `memex_memory_search` AND `memex_note_search` in parallel (no expansion).
  2. **If insufficient**: retry BOTH with `expand_query=true` for LLM-powered query expansion.
  3. **If still nothing**: abstain — do not guess.
  `memory_search` returns individual facts/observations across notes — use it to find cross-cutting patterns, contradictions, and specific claims buried inside large documents. `note_search` returns whole notes ranked by relevance — use it to find source documents. �� `memex_get_notes_metadata` after memory_search (skip after note_search — metadata inline) → read via `memex_get_page_indices` + `memex_get_nodes` (use `memex_read_note` only when total_tokens < 500)
- **Broad / panoramic** ("what do you know about X?", "overview of X") → `memex_survey(query)` for auto-decomposed parallel search; or entity exploration AND search in parallel for manual control
- **Vault overview** ("what's in this vault?") → `memex_get_vault_summary` + `memex_survey` in parallel
- **Assets** → when `has_assets: true`: `memex_list_assets` → `memex_get_resources`. Use images as visual input. Reproduce diagrams as Mermaid/ASCII. NEVER skip.

Search results include `related_notes` and `links` — use these for inline relationship data.

Session start context is automatic. Do NOT redundantly search at session start.

## Memex capture — MANDATORY

Keep notes concise (hard max: 300 tokens). No per-file changelogs.

Call `memex_add_note` (background: true, author: "claude-code") when:

1. Completed a multi-step task (what was done, decisions, outcome)
2. Diagnosed a bug root cause (symptom, cause, fix)
3. Made/discovered an architectural decision (decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

### Capture exclusions

Do NOT save any of the following:

- Per-file changelogs or command sequences
- Information derivable from reading the code
- Git history (use `git log`)
- The fix itself — save the insight about why it was needed
- Ephemeral task details (which files were edited, in what order)

## Memex KV store

- `memex_kv_write(value, key)` / `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()`
- Keys MUST use namespace prefix: `global:`, `user:`, `project:<id>:`, or `app:<id>:`
- Proactively store user preferences and conventions via `memex_kv_write`
- Deletion is user-only — do NOT delete KV entries

## Memex citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim
2. Reference list with type prefix: `[note]` title + note ID, `[memory]` title + memory ID + source note ID, `[asset]` filename + note ID

## Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories

## Memex prohibitions

- NEVER use `memex_recent_notes` for discovery
- NEVER fabricate Note/Node/Unit IDs — only use IDs from tool output
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata already inline)
- NEVER use `memex_read_note` on notes over 500 tokens — use page_indices + get_nodes
- NEVER create diagrams without first checking assets
- NEVER present Memex data without citations
