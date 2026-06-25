---
name: remember
description: "Save information to Memex long-term memory. Routes how-tos/worked-episodes to a case, preferences/conventions to KV, or facts/decisions to a note."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex

1. **Content**: use `$ARGUMENTS` if provided; otherwise infer the most important persistable context.

2. **Route by shape, NOT by trigger word** — this is the most important step. Pick the storage layer first:
   - **Preferences / conventions / settings** ("I prefer X", "we use Y in this repo", "for Claude Code: dark theme", "company-wide: Python 3.12") → `memex_kv_put` with the scope-qualifier-derived namespace (`user:`, `project:<id>:`, `app:<app-id>:`, `global:`) and an ordinary `<scope>:<field>` key. See KV-namespace rules in the system prompt. **Do NOT save these as notes.**
   - **Learned how-tos / worked episodes** (you just finished a multi-step task, diagnosed a bug, resolved an incident, or worked out how to do something — "next time I hit this I'd want these steps back") → **`memex_case_submit`** — composes the episode template (trigger / situation / actions / outcome / lesson) and files it as a case NOTE in a hidden system vault. **The system DERIVES the procedure (and any higher-order strategy) from the cases you submit — you never author a procedure or strategy directly.** Pass `case_of=<procedure-id>` when you already have a procedure for this how-to (probe with `memex_procedural_get_by_identity` — see "Procedural memory" below).
   - **Facts / decisions / context / observations** that belong as a paragraph → `memex_add_note` (or `memex_append_note` to extend an existing note). Use the note-format guidance below.
   - **Routine / one-off** with no future value → save nothing.

3. **Note format** (only when step 2 picked `memex_add_note`):
   - **title**: concise, ≤10 words
   - **markdown_content**: specific enough to be useful without the original conversation, 5-15 lines
   - **description**: one-sentence summary, ≤250 words
   - **author**: `"claude-code"`
   - **tags**: include `"manual-capture"` + 1-3 topic tags (the plugin auto-injects ambient tags — do not repeat them)

4. **Template** (note path only, for *structured* content — a decision, brief, RFC, reflection): suggest or select a template before writing. Skip for short/quick captures.
   - Call `memex_list_templates` — it returns a **markdown string** (parse the `- **slug** [source] — name: description` bullets), not JSON.
   - If one template clearly fits (e.g. an architecture decision → `architectural_decision_record`), auto-select it; otherwise present a short `AskUserQuestion` pick-list of the top candidates plus a "plain note" option.
   - Fetch the scaffold with `memex_get_template(type=<slug>)` — note the param is named **`type`**, not `slug`, despite the tool's own hint.
   - `template=<slug>` is provenance/filter metadata only — it does NOT auto-structure the note, so write the body to match the scaffold yourself, then `memex_add_note(..., template=<slug>)`.

5. **Save**: call the tool picked in step 2. For `memex_add_note` the plugin auto-defaults `background: true`; pass it explicitly only if you need synchronous ingestion.

## Auto-injected metadata

A `PreToolUse` hook adds ambient tags (surface / session / project, git branch+sha+repo+dirty, model, plugin version) to every `memex_add_note` and `memex_case_submit` call, and on notes defaults `background=true` and `vault_id=<active_vault>` when absent — so don't set these yourself. Your explicit `tags` are merged (deduplicated, never stripped) and an explicit `background: false` is preserved.

## Deprioritize vs archive

- `memex_memory_deprioritize(unit_id, reason)` — non-destructive. Lowers retrieval rank; reversible via `memex_memory_restore`.
- Archive (CLI-only) — destructive, removes from entity graph. Prefer deprioritize.

## User reports an issue fixed

Follow the 5-step resolution flow in the system prompt (§"5-step resolution flow", arriving via the SessionStart hook). The paired-write shape on failure is:

`memex_record_outcome(units=[{unit_id, verb: "not_helpful", reason}])` AND `memex_memory_deprioritize(unit_id, reason)`

Bare `success=true`/`success=false` without `units` returns HTTP 400.

## Procedural memory

"How to do X" knowledge lives on the **procedural plane** — distinct from notes (long-form prose) and KV (preferences / bindings). **The agent never authors a procedure or strategy directly: you file worked episodes via `memex_case_submit`, and the system DERIVES the procedure (and any higher-order strategy) from those cases.** Procedures carry an identity anchor `(kind, scope, verb, context)`, and arrive as pinned cards in your SessionStart briefing.

**Tools** (exposed automatically by the plugin's `.mcp.json`):

| Tool | When |
| --- | --- |
| `memex_case_submit` | File a worked episode as a case NOTE (hidden system vault). Required: `title`, `trigger`, `outcome`, `scope` (`global`/`project:<id>`/`app:<id>`), `scope_reasoning` (one line) — the call 422s without `scope`/`scope_reasoning`. Pass `case_of=<procedure-id>` when known; contested assignments land in the lint queue (`assignment.mode="escalated"`). **This is the agent's ONLY procedural write.** |
| `memex_procedural_get_by_identity` | Probe by `(kind, scope, verb, context)`. Returns `null` on miss — the cheap "do we already have a procedure for this?" check. If it returns an entry, pass its id as `case_of` when you submit the case. |
| `memex_procedural_get` | Fetch a single derived procedure by UUID. |
| `memex_procedural_search` | Hybrid BM25 + vector search (RRF-merged) over derived procedures. Required: `query`. |

**Scope grammar** (anchors + pin-chain context):

- `global` — cross-project convention.
- `project:<id>` — one project.
- `app:<id>` — one application (e.g. `app:claude-code`).
- There is NO `user` scope — per-user briefing curation is done by pinning (an operator surface), not by scoping entries.

**The probe-then-file pattern** (the load-bearing operational pattern):

```
# 1. probe for an existing derived procedure on the anchor
existing = memex_procedural_get_by_identity(kind="procedure", scope="global", verb="rotate", context="creds")
# 2. file the worked episode; link it to the procedure if one already exists
case_of = existing["id"] if existing is not None else None
memex_case_submit(title="Rotated project API creds", trigger="rotating the project API credentials",
                  outcome="...", scope="global", scope_reasoning="creds rotation is a cross-project routine",
                  case_of=case_of)
# scope + scope_reasoning are REQUIRED (422 without them).
# the system derives/updates the procedure from this case — you do not write one.
```

**Briefing**: pinned procedure cards arrive automatically inside the SessionStart briefing (the plugin passes `--app claude-code` so the `app:claude-code` pin context is included). There is no briefing tool to call.

**When to choose case vs. KV vs. note**:

- **Case** (`memex_case_submit`) — "what just happened, and next time I'd want these steps back": a how-to / worked episode. The system derives the procedure.
- **KV** (`<scope>:<field>`) — a preference / convention / binding.
- **Note** — long-form prose, context, decisions, history. NOT a procedure.

## Consolidation

- `memex_memory_summarize_node(entity_id, scope)` — synchronous reflection. `'incremental'` (default) or `'full'` (capped 100 units). Rate-limited: 1 call per (entity, vault) per 60s.
- `memex_memory_reconsolidate(entity_id, vault_id)` — entity-scoped contradiction detection + reflection.
- `memex_memory_consolidate(vault_id, dry_run)` — vault-scoped batch deprioritization. Use sparingly.
