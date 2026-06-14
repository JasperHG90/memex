---
name: procedure
description: "Recall the step-by-step how-to for a task from Memex's procedural plane. Use whenever you ask 'how do we X?', 'how do I X?', 'what's our process/checklist for X?' (deploy, release, rotate creds, migrate) and want the concrete steps — NOT the general approach (that's /strategy). Reach for this before improvising a multi-step task you may have done before."
argument-hint: "[how-to query, e.g. 'how do we cut a release']"
---

# /procedure — Recall a derived procedure

"How do we do X?" knowledge lives on the **procedural plane** — derived procedures, distinct from notes (long-form prose) and KV (preferences / bindings). This skill goes straight there. It is a dedicated front door for what `/recall` already routes how-to queries to; use `/procedure` when you know you want a procedure and don't want to think about routing.

## 1. Query source

`$ARGUMENTS` is the task you want the procedure for. If it's empty, ask the user what they're trying to do — don't guess.

## 2. Pinned cards first

The highest-value procedures already arrive in your SessionStart briefing as pinned cards (pin chain `global → project:<id> → app:claude-code`). If the briefing already carries a card that answers the query, surface it from there before searching — no tool call needed.

## 3. Search the procedural plane

- **Fuzzy / natural-language query** → `memex_procedural_search(query="...", kind="procedure")` (hybrid BM25 + vector, RRF-merged). Prefer this when you don't know the exact anchor.
- **Exact anchor lookup** → `memex_procedural_get_by_identity(kind="procedure", scope="<scope>", verb="<verb>", context="<context>")`. For a **procedure**, both `verb` AND `context` are required — omit either and the tool returns an error (the anchor is `(kind, scope, verb, context)`). Returns the entry, or `null` on a clean miss.
- Scope grammar: `global` (cross-project), `project:<id>` (one project), `app:<id>` (one app, e.g. `app:claude-code`). There is no `user` scope.
- Use `memex_procedural_get(entry_id)` when you already hold the UUID (e.g. from a briefing card).

## 4. Present

Render the procedure's steps plainly and **cite the entry id** for the load-bearing claim. If nothing matches, say so in one line — don't fall back to `memex_memory_search` and pass a note off as a procedure, and don't invent steps.

There is no procedure-write tool: procedures are **derived** from the cases people file (`/case`, `/remember`, `/extract-case`). If the right procedure doesn't exist yet, that's the signal to capture the next worked episode as a case.
