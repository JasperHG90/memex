---
name: remember
description: "Save information to Memex long-term memory. Captures text or infers important context as a persistent note."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex

1. **Content**: use `$ARGUMENTS` if provided; otherwise infer the most important persistable context.

2. **Format**:
   - **title**: concise, ≤10 words
   - **markdown_content**: specific enough to be useful without the original conversation, 5-15 lines
   - **description**: one-sentence summary, ≤250 words
   - **author**: `"claude-code"`
   - **tags**: include `"manual-capture"` + 1-3 topic tags (the plugin auto-injects ambient tags — do not repeat them)

3. **Surface**: note (`memex_add_note` / `memex_append_note`) for facts/decisions/context. Use `procedure:` KV for how-tos.

4. **Template**: `memex_list_templates` → `memex_get_template(slug)` → `memex_add_note(..., template=slug)`. Skip for short captures.

5. **Save**: call `memex_add_note`. The plugin auto-defaults `background: true`; pass it explicitly only if you need synchronous ingestion.

## Auto-injected metadata

A `PreToolUse` hook augments every `memex_add_note` call with ambient capture metadata so you don't have to repeat it on each invocation:

| Tag | When |
| --- | --- |
| `surface:claude-code` | Always |
| `session:<note_key>` | Always (groups all notes from one CC session) |
| `project:<project_id>` | Always (cross-vault discoverability) |
| `git:branch=<branch>` | When inside a git repo |
| `git:sha=<short>` | When a commit exists |
| `git:repo=<owner/name>` | When `origin` remote is set |
| `git:dirty` | When the working tree has uncommitted changes |
| `claude:model=<id>` | When SessionStart cached the model identifier |
| `cc:plugin=<version>` | Plugin version provenance |

The hook also defaults `background=true` and `vault_id=<active_vault>` when those fields are absent. Pre-existing values you supply are preserved — passing `background: false` for synchronous ingestion still works.

To opt out for a specific call, pass an explicit `tags` array containing only what you want. The hook merges (deduplicating); it does not strip values.

## Deprioritize vs archive

- `memex_memory_deprioritize(unit_id, reason)` — non-destructive. Lowers retrieval rank; reversible via `memex_memory_restore`.
- Archive (CLI-only) — destructive, removes from entity graph. Prefer deprioritize.

## 5-step resolution flow (user reports issue fixed)

1. **Disambiguate** — if scope ambiguous, ASK.
2. **Route** — title → `memex_find_note`; content → `memex_memory_search`. Pick: (A) entity-anchored, (B) cross-note semantic (top_k≥30), or (C) single-note PageIndex.
3. **LLM-judge** — read candidate unit bodies, pick fix-relevant subset. Never bulk-write.
4. **Paired writes**: `memex_record_outcome(success=false)` AND `memex_memory_deprioritize(reason=...)`.

## Procedure KV

For how-tos, write to `procedure:<verb>:<context-tag>`:
- Save: `memex_kv_write(value=..., key="procedure:<verb>:<context-tag>")`
- Read: `memex_kv_get(key)`; history: `memex_kv_get(key, include_history=true)`
- After USE: `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`

## Consolidation

- `memex_memory_summarize_node(entity_id, scope)` — synchronous reflection. `'incremental'` (default) or `'full'` (capped 100 units). Rate-limited: 1 call per (entity, vault) per 60s.
- `memex_memory_reconsolidate(entity_id, vault_id)` — entity-scoped contradiction detection + reflection.
- `memex_memory_consolidate(vault_id, dry_run)` — vault-scoped batch deprioritization. Use sparingly.
