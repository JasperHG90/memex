---
name: remember
description: "Save information to Memex long-term memory. Routes to KV for preferences/conventions/settings/procedures, or to a note for facts/decisions/context."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex

1. **Content**: use `$ARGUMENTS` if provided; otherwise infer the most important persistable context.

2. **Route by shape, NOT by trigger word** — this is the most important step. Pick the storage layer first:
   - **Preferences / conventions / settings** ("I prefer X", "we use Y in this repo", "for Claude Code: dark theme", "company-wide: Python 3.12") → `memex_kv_put` with the scope-qualifier-derived namespace (`user:`, `project:<id>:`, `app:<app-id>:`, `global:`). See KV-namespace rules in the system prompt. **Do NOT save these as notes.**
   - **Learned how-tos / procedures** (how to rotate creds, how to deploy, audit checklist) → **`memex_procedural_create`** on the V7 procedural plane (see "Procedural memory" below). The KV-namespace procedure convention (`<scope>:procedure:<verb>:<context>`) is the legacy path; prefer the procedural plane when the V7 MCP tools are available — they carry the identity anchor `(kind, scope, verb, context)`, versioned writes, lifecycle states, and pin-chain briefings.
   - **Cases / trigger signals** (a recurring failure pattern the agent should watch for: "when CI returns 500 after step 3", "when Stripe webhook delivers duplicate events") → **`memex_procedural_create` with `kind="case"` and a `trigger` field**. Cases are pin-chain-friendly and surface in `memex_procedural_briefing_cards`.
   - **Strategies** (a procedure paired with a trigger: "on prod outage: do X") → **`memex_procedural_create` with `kind="strategy"`** — verb+context for the procedure, trigger for the auto-fire condition.
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

## Procedural memory (V7)

The V7 procedural plane is the canonical home for "how to do X" knowledge — distinct from notes (long-form prose) and KV (preferences / bindings). It carries an identity anchor, versioned writes, and a pin-chain briefing surface.

**8 tools** (all available via the `memex` MCP server — exposed automatically by the plugin's `.mcp.json`):

| Tool | When |
| --- | --- |
| `memex_procedural_create` | Write a new entry. Required: `kind`, `scope`, `title`, `summary`. Procedure+strategy REQUIRE `verb`+`context`; case MUST omit both and supplies a `trigger` instead. 409 on identity-anchor collision — use `memex_procedural_get_by_identity` to probe first. |
| `memex_procedural_upsert` | Idempotent write on the anchor. Same shape as `create`. Use when retrying after an uncertain 409. |
| `memex_procedural_get` | Fetch a single entry by UUID. |
| `memex_procedural_get_by_identity` | Look up by `(kind, scope, verb, context)`. Returns `null` on miss — the cheap "did we already learn this?" probe. **Always call this before `create` to avoid 409s.** |
| `memex_procedural_update` | Mutate an entry in place (appends a version row). At least one of `title`, `summary`, `body`, `trigger`, `tags`, `extra_metadata`, `status` must be supplied. The identity anchor is immutable. |
| `memex_procedural_deprecate` | Soft-deprecate (status → `deprecated`). Optional `superseded_by_id` for the supersession chain. |
| `memex_procedural_search` | Hybrid BM25 + vector search (RRF-merged). Required: `query`. Optional: `kind`, `scope`, `status` (default `"published"`), `top_k`, `include_pin_chain`, `pin_contexts`, `bm25_weight`. |
| `memex_procedural_briefing_cards` | Pin-chain briefing cards. Required: `context_keys` (non-empty list). One card per pinned entry. Use to load the "what you should know going in" block at session start. |

**Kind matrix** (the load-bearing piece):

- `kind="case"` — a trigger signal. `verb` and `context` MUST be omitted. `trigger` is required. E.g. "When CI returns 500 after step 3: do X" → case with `trigger="CI returns 500 after step 3"`.
- `kind="procedure"` — a how-to. `verb` and `context` REQUIRED. E.g. `verb="rotate", context="creds"`.
- `kind="strategy"` — a procedure + trigger. `verb` and `context` REQUIRED (the procedure part), `trigger` optional (the auto-fire condition).

**Scope matrix** (the pin chain, most-specific wins):

- `global` — cross-vault convention.
- `user` — single-user.
- `project:<id>` — one project.
- `app:<id>` — one application (e.g. `app:claude-code`).

**Write-before-read rule** (the load-bearing operational pattern):

```
# 1. probe for an existing entry on the anchor
existing = memex_procedural_get_by_identity(kind="procedure", scope="global", verb="rotate", context="creds")
# 2. if None → create; if not None → update (or upsert if unsure)
if existing is None:
    memex_procedural_create(kind="procedure", scope="global", verb="rotate", context="creds", ...)
else:
    memex_procedural_update(entry_id=existing["id"], title=..., body=...)
```

**When to choose procedural vs. KV vs. note**:

- **Procedural** — "how to do X" with an identity anchor. Discoverable by search. Briefing-eligible.
- **KV** (`<scope>:procedure:<verb>:<context>`) — legacy procedure path. Still works but lacks search, briefings, and lifecycle.
- **Note** — long-form prose, context, decisions, history. NOT a procedure.

## Consolidation

- `memex_memory_summarize_node(entity_id, scope)` — synchronous reflection. `'incremental'` (default) or `'full'` (capped 100 units). Rate-limited: 1 call per (entity, vault) per 60s.
- `memex_memory_reconsolidate(entity_id, vault_id)` — entity-scoped contradiction detection + reflection.
- `memex_memory_consolidate(vault_id, dry_run)` — vault-scoped batch deprioritization. Use sparingly.
