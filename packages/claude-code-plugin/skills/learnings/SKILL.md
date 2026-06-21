---
name: learnings
description: "Distill the durable learnings from the current session and save them to Memex, routing each into the right memory layer by shape. Use when the user says 'capture what I learned', 'save the lessons from this session', 'what should we remember from this', or wants the takeaways from the whole conversation kept — as opposed to one named episode (`/case`) or one supplied fact (`/remember`)."
argument-hint: "[optional: focus area]"
---

# /learnings — Distill the session's durable learnings

You have been invoked via the `/learnings` slash command.

Review the conversation, pull out the learnings worth keeping, and route **each one** into the memory layer that fits its shape. This is the whole-session counterpart to `/remember` (which routes one supplied item) and `/case` (which files one named episode). It does NOT write a reflection note or edit `CLAUDE.md` — durable learnings live in Memex, not in local config files.

## 1. Find the durable learnings

Read back over the session (use `$ARGUMENTS` to focus on an area if given). Keep only learnings that are:

- **Durable** — true across future sessions, not a one-off fix for this exact moment.
- **Non-derivable** — not obvious from reading the code or git history.
- **Worth retrieving** — you (or another agent) would genuinely want it back later.

Drop transient details, restatements of what the code already says, and anything that "just worked." If nothing clears the bar, that's a valid result — say so and stop.

## 2. Route each learning by shape

For every learning that survived step 1, pick exactly **one** layer — same shape-first routing as `/remember`, applied per item:

- **Reusable how-to / worked episode** (you worked out HOW to do or fix something — trigger + actions + outcome) → `memex_case_submit` (trigger / situation / actions / outcome / lesson, plus a required `scope` — `global` / `project:<id>` / `app:claude-code` — and a one-line `scope_reasoning`). Probe `memex_procedural_get_by_identity(kind="procedure", scope=…, verb=…, context=…)` first; if it returns an entry, pass its id as `case_of`. The system derives the procedure — you never author one.
- **Preference / convention / setting** ("we use X here", "always Y before Z") → `memex_kv_put` with a scope-qualified key (`user:` / `project:<id>:` / `app:claude-code:` / `global:`), scope chosen by cue (the `<app>` cue beats "I"/"my").
- **Durable fact / decision / insight** that belongs as prose → `memex_add_note` (concise, 5–15 lines, `author="claude-code"`, tags include `"learnings"` + 1–3 topic tags).

The plugin's `PreToolUse` hook auto-injects ambient tags on `memex_add_note` and `memex_case_submit` (and, on `memex_add_note`, defaults `background=true` and `vault_id`) — don't hand-set those.

## 3. Distinct from neighbouring skills

- `/case` files **one** named episode you describe; `/learnings` scans the **whole** session for several takeaways.
- `/remember` routes **one** supplied item (`$ARGUMENTS`); `/learnings` discovers them from the conversation.
- `/handoff` writes a *where-the-work-stands* summary note; `/learnings` extracts *what-to-remember* and routes by shape — they can both be useful at session end.

## 4. Report

One line per learning: what was captured and which layer it went to (case / KV / note). If nothing was durable enough to keep, say that plainly.
