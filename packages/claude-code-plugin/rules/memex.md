## Search query formulation

<constraint name="search-queries" priority="high">
ALWAYS formulate search queries as natural language, NEVER as keyword lists.
ALWAYS preserve proper nouns, amounts, dates, qualifiers from the original question.
ALWAYS search for the subject/activity, NOT the answer type.
</constraint>

## Memex retrieval routing

- **Title known** → `memex_find_note(query="fragment")` → `memex_get_page_indices` + `memex_get_nodes`
- **Relationships** → `memex_list_entities` → `memex_get_entity_cooccurrences` → `memex_get_entity_mentions`
- **Content lookup** → `memex_memory_search` AND `memex_note_search` in parallel. Retry with `expand_query=true` if insufficient. Abstain if still nothing. `memory_search` returns facts across notes; `note_search` returns source docs. After `memory_search`, call `memex_get_notes_metadata`; skip after `note_search` (metadata inline). Read via `memex_get_page_indices` + `memex_get_nodes` (`memex_read_note` only when `total_tokens < 500`).
- **Broad/panoramic** → `memex_survey(query)` (auto-decomposed parallel search) or entity exploration + search in parallel
- **Vault overview** → `memex_get_vault_summary` + `memex_survey` in parallel
- **Assets** → `memex_list_assets` → `memex_get_resources` when `has_assets: true`. Reproduce diagrams as Mermaid/ASCII. NEVER skip.

Search results include `related_notes` and `links` — use these for inline relationship data.

Do NOT redundantly search at session start — context is automatic.

## Memex capture — MANDATORY

Hard max: 300 tokens per note. No per-file changelogs.

Call `memex_add_note` (background: true, author: "claude-code") when:
1. Completed a multi-step task (what, decisions, outcome)
2. Diagnosed a bug root cause (symptom, cause, fix)
3. Made/discovered an architectural decision (decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

Do NOT save: per-file changelogs, info derivable from code, git history, the fix itself (save the insight why), ephemeral task details.

## Memex KV store

- `memex_kv_write(value, key)` / `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()`
- Keys MUST start with: `global:`, `user:`, `project:<id>:`, `app:<id>:`, or `procedure:<verb>:<context-tag>`
- `procedure:` namespace: compact, learned how-tos. Write via `memex_kv_write`, read active value via `memex_kv_get(key)`, inspect envelope (active value + version + 5-version history) via `memex_kv_get(key, include_history=true)`. Track outcomes via `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`.
- Proactively store user preferences and conventions via `memex_kv_write`
- Deletion is user-only — do NOT delete KV entries

## Memex citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim
2. Reference list: `[note]` title + note ID, `[memory]` title + memory ID + source note ID, `[asset]` filename + note ID

## Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories

## Memex prohibitions

- NEVER use `memex_recent_notes` for discovery
- NEVER fabricate Note/Node/Unit IDs — only use IDs from tool output
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata inline)
- NEVER use `memex_read_note` on notes over 500 tokens — use page_indices + get_nodes
- NEVER create diagrams without first checking assets
- NEVER present Memex data without citations
