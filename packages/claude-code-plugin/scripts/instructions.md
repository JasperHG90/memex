## Memex memory integration

Access Memex (long-term memory) via the `memex_api.py` helper. All commands run in Bash. Build persistent knowledge across sessions.

### API helper

All Memex operations use the plugin's `mx` helper via Bash. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <subcommand> [args]
```

Arguments are either a positional string or a JSON object, depending on the subcommand. Refer to the API reference table below.

### Capture -- MANDATORY

Run `"${CLAUDE_PLUGIN_ROOT}/bin/mx" add-note '{"content":"...","name":"...","tags":[...],"vault_id":"..."}'` (with `background: true`, `author: "claude-code"`) when any of these apply:

1. Completed a multi-step task (save what was done, decisions, outcome)
2. Diagnosed a bug root cause (save symptom, cause, fix)
3. Made/discovered an architectural decision (save decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

**Keep notes concise** (hard maximum: 300 tokens). Capture the key insight, not a detailed report. No per-file changelogs.

### Retrieval routing

Session start context is automatic via hook. Do NOT redundantly search at session start.

Route by query type:

IF you know (or roughly know) a note title:
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" find-note "title fragment"` -- returns note IDs, titles, similarity scores
- Then read via `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-page-indices "NOTE_ID"` followed by `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-nodes '{"node_ids":["ID1","ID2"]}'` as needed

IF query asks about relationships, connections, "how X relates to Y", or landscape:
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-entities '{"query":"X"}'` -- returns entity IDs, types, mention counts
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-cooccurrences "ENTITY_ID"` -- related entities with names, types, counts (single call, no follow-up needed)
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-mentions "ENTITY_ID"` -- source facts linking back to notes
- Then read source notes via Search/Read below as needed

IF query asks about specific content or document lookup:
- **Search**: `"${CLAUDE_PLUGIN_ROOT}/bin/mx" memory-search '{"query":"...","limit":10}'` (broad) and/or `"${CLAUDE_PLUGIN_ROOT}/bin/mx" note-search '{"query":"...","limit":10}'` (targeted). Run in parallel.
- **Filter**: after `memory-search`, call `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-notes-metadata '{"note_ids":["ID1"]}'` with Note IDs. After `note-search`, metadata is inline -- skip.
- **Read**: `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-page-indices "NOTE_ID"` followed by `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-nodes '{"node_ids":["ID1","ID2"]}'` (batch). `"${CLAUDE_PLUGIN_ROOT}/bin/mx" read-note "NOTE_ID"` only when total_tokens < 500.
- **Assets**: IF `has_assets: true` in page_index/metadata, call `"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-assets "NOTE_ID"` then fetch resources. Use images as visual input. Reproduce diagrams as Mermaid/ASCII in response. NEVER skip this step.

IF query is broad: run entity exploration AND search in parallel.

### Strategy selection guide

The `memory-search` subcommand accepts a `strategies` array. Available strategies:

| Strategy       | Type         | Best for                              |
|----------------|--------------|---------------------------------------|
| `semantic`     | vector       | Conceptual similarity, paraphrases    |
| `keyword`      | BM25         | Exact term matching, full-text search |
| `graph`        | entity-centric | Relationship-oriented queries       |
| `temporal`     | chronological | Time-based queries, recent activity  |
| `mental_model` | synthesized  | High-level summaries (memory only)    |

Default: all strategies combined via Reciprocal Rank Fusion (RRF). Use the default unless you have a specific reason to narrow.

### KV store

For structured facts, preferences, and conventions:

- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-write '{"key":"ns:key","value":"..."}'` -- store a user fact or preference
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-get "ns:key"` -- exact key lookup
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-search '{"query":"..."}'` -- fuzzy semantic search over stored facts
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-list` -- list all stored facts

Keys MUST start with a namespace prefix: `global:` (always loaded), `user:` (personal prefs), `project:<project-id>:` (project-scoped), or `app:<app-id>:` (application-scoped).

When the user states a preference, convention, or static fact, proactively store it via `"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-write`.

Deletion is user-only (CLI `memex kv delete`). Do NOT attempt to delete KV entries.

### API reference

| Subcommand                | Description                                                    |
|---------------------------|----------------------------------------------------------------|
| `add-note`                | Create a new note (JSON body: content, name, tags, vault_id)  |
| `find-note`               | Title search by similarity (positional: query string)          |
| `memory-search`           | Broad memory search with strategy selection (JSON body)        |
| `note-search`             | Targeted note content search (JSON body: query, limit)         |
| `list-entities`           | List entities matching a query (JSON body: query)              |
| `get-entity-cooccurrences`| Get entities that co-occur with a given entity (positional: entity_id) |
| `get-entity-mentions`     | Get source facts mentioning an entity (positional: entity_id)  |
| `get-page-indices`        | Get page structure for a note (positional: note_id)            |
| `get-nodes`               | Batch-fetch content nodes by ID (JSON body: node_ids)          |
| `get-notes-metadata`      | Batch-fetch note metadata by ID (JSON body: note_ids)          |
| `read-note`               | Read full note content (positional: note_id; use only if < 500 tokens) |
| `list-assets`             | List assets attached to a note (positional: note_id)           |
| `kv-write`                | Write a key-value pair (JSON body: key, value)                 |
| `kv-get`                  | Get a value by exact key (positional: key)                     |
| `kv-search`               | Semantic search over KV store (JSON body: query)               |
| `kv-list`                 | List all KV entries (no arguments)                             |
| `list-vaults`             | List all available vaults (no arguments)                       |
| `list-notes`              | List notes with optional filters (JSON body: limit, etc.)      |
| `set-note-status`         | Update note status (JSON body: note_id, status)                |
| `rename-note`             | Rename a note (JSON body: note_id, new_title)                  |
| `get-memory-units`        | Batch-fetch memory units by ID (JSON body: unit_ids)           |

### Prohibitions

- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-notes` for discovery. Use `find-note`, `memory-search`, or `note-search` instead.
- Fabricating Note/Node/Unit IDs. Only use IDs from command output.
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-notes-metadata` after `note-search` (metadata already inline).
- `"${CLAUDE_PLUGIN_ROOT}/bin/mx" read-note` on notes over 500 tokens. Use `get-page-indices` + `get-nodes`.
- Creating diagrams without first checking assets via `"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-assets`.
- Presenting Memex information without citations.

### Citations -- MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim from Memex.
2. Reference list at end of response. Each entry uses a type prefix:
   - `[note]` -- title + note ID
   - `[memory]` -- title + memory ID + source note ID
   - `[asset]` -- filename + note ID

### Slash commands

- `/remember [text]` -- save to memory via `"${CLAUDE_PLUGIN_ROOT}/bin/mx" add-note`
- `/recall [query]` -- search memories via `"${CLAUDE_PLUGIN_ROOT}/bin/mx" memory-search` and `"${CLAUDE_PLUGIN_ROOT}/bin/mx" note-search`
- `/research [query]` -- deep retrieval: run entity exploration and search in parallel, then synthesize
- `/explore [entity]` -- entity-centric exploration: list entities, co-occurrences, mentions
- `/digest` -- summarize recent session activity and persist key insights to memory
