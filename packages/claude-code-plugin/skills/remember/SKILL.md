---
name: remember
description: "Save information to Memex long-term memory. Routes to KV for preferences/conventions/settings/procedures, or to a note for facts/decisions/context."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex

1. **Content**: use `$ARGUMENTS` if provided; otherwise infer the most important persistable context.

2. **Route by shape, NOT by trigger word** — this is the most important step. Pick the storage layer first:
   - **Preferences / conventions / settings** ("I prefer X", "we use Y in this repo", "for Claude Code: dark theme", "company-wide: Python 3.12") → `memex_kv_put` with the scope-qualifier-derived namespace (`user:`, `project:<id>:`, `app:<app-id>:`, `global:`). See KV-namespace rules in the system prompt. **Do NOT save these as notes.**
   - **Learned how-tos / procedures** → `memex_kv_put` with `<scope>:procedure:<verb>:<context-tag>` key (scope = `global` default, or `project:<id>` on explicit project cue). Each write appends a new version; prior versions remain queryable via `memex_kv_get(key, include_history=true)`.
   - **Facts / decisions / context / observations** that belong as a paragraph → `memex_add_note` (or `memex_append_note` to extend an existing note). Use the note-format guidance below.

3. **Note format** (only when step 2 picked `memex_add_note`):
   - **title**: concise, ≤10 words
   - **markdown_content**: specific enough to be useful without the original conversation, 5-15 lines
   - **description**: one-sentence summary, ≤250 words
   - **author**: `"claude-code"`
   - **tags**: include `"manual-capture"` + 1-3 topic tags (the plugin auto-injects ambient tags — do not repeat them)

4. **Template** (note path only): `memex_list_templates` → `memex_get_template(slug)` → `memex_add_note(..., template=slug)`. Skip for short captures.

5. **Save**: call the tool picked in step 2. For `memex_add_note` the plugin auto-defaults `background: true`; pass it explicitly only if you need synchronous ingestion.

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

## User reports an issue fixed

Follow the 5-step resolution flow in the system prompt (§"5-step resolution flow", arriving via the SessionStart hook). The paired-write shape on failure is:

`memex_record_outcome(units=[{unit_id, verb: "not_helpful", reason}])` AND `memex_memory_deprioritize(unit_id, reason)`

Bare `success=true`/`success=false` without `units` returns HTTP 400.

## Procedure KV

For how-tos, write to `<scope>:procedure:<verb>:<context-tag>` (scope = `global` default, `project:<id>` on explicit project cue):
- Save: `memex_kv_put(value=..., key="global:procedure:<verb>:<context-tag>")` or `memex_kv_put(value=..., key="project:<id>:procedure:<verb>:<context-tag>")` — each write appends a new version; prior versions remain in the history envelope.
- Read: `memex_kv_get(key)` for the active value; `memex_kv_get(key, include_history=true)` for the full envelope.

## Consolidation

- `memex_memory_summarize_node(entity_id, scope)` — synchronous reflection. `'incremental'` (default) or `'full'` (capped 100 units). Rate-limited: 1 call per (entity, vault) per 60s.
- `memex_memory_reconsolidate(entity_id, vault_id)` — entity-scoped contradiction detection + reflection.
- `memex_memory_consolidate(vault_id, dry_run)` — vault-scoped batch deprioritization. Use sparingly.
