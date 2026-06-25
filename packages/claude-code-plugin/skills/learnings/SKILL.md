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

## 2. Route each learning by shape — notes are the default home

A distilled learning is an **insight**: a finding, a decision, a conclusion — *what we now know / what's true / what we settled*. **Insights are note-shaped, and an insight MUST be written with `memex_add_note`.** That is the whole point of `/learnings`, and the note is the default destination for everything that survives step 1.

There are exactly **two narrow carve-outs** — and a learning only leaves the note path when it genuinely matches one of them in shape:

- **Reusable how-to / worked episode** — you worked out HOW to do or fix something (trigger + actions + outcome), and you'd re-run those steps next time → `memex_case_submit` (trigger / situation / actions / outcome / lesson, plus a required `scope` — `global` / `project:<id>` / `app:claude-code` — and a one-line `scope_reasoning`). Probe `memex_procedural_get_by_identity(kind="procedure", scope=…, verb=…, context=…)` first; if it returns an entry, pass its id as `case_of`. The system derives the procedure — you never author one.
- **Preference / convention / setting** — ONE static binding ("we use X here", "always Y before Z") → `memex_kv_put` with a scope-qualified key (`user:` / `project:<id>:` / `app:claude-code:` / `global:`), scope chosen by cue (the `<app>` cue beats "I"/"my").

Everything else — every finding, diagnosis, "the real constraint is X not Y", "approach A beats B", "this is why Z happens" — is an insight → `memex_add_note` (concise, 5–15 lines, `author="claude-code"`, tags include `"learnings"` + 1–3 topic tags). When an item could be read as either an insight or a carve-out, **write the note** (and add the case/KV too if it also genuinely fits — they are not mutually exclusive).

<critical_constraint name="insight_must_be_a_note">
A distilled insight is NOT captured until it is a `memex_add_note`. None of these count as its home:
- a `/handoff` note (session-anchored *status* that ages out — not a durable insight);
- a `memex_kv_put` (one static setting, not a finding);
- a `memex_case_submit` (a reusable procedure, not a conclusion).
Deciding an insight was "already captured" by a handoff, a KV entry, or a case is the failure this skill exists to prevent. The insight rides in a note, full stop.
</critical_constraint>

The plugin's `PreToolUse` hook auto-injects ambient tags on `memex_add_note` and `memex_case_submit` (and, on `memex_add_note`, defaults `background=true` and `vault_id`) — don't hand-set those.

## 3. Verify before reporting

Self-check: if step 1 surfaced **any** durable insight/finding/lesson and you wrote **zero** `memex_add_note` calls this run, you mis-routed — you shunted insights into KV/cases/handoffs. Stop, re-route them as notes, then report. (The only way to legitimately write zero notes is if *every* surviving learning was a pure reusable how-to or a pure one-line setting — rare.)

## 4. Distinct from neighbouring skills

- `/case` files **one** named episode you describe; `/learnings` scans the **whole** session for several takeaways.
- `/remember` routes **one** supplied item (`$ARGUMENTS`); `/learnings` discovers them from the conversation.
- `/handoff` writes a *where-the-work-stands* status note that ages out; `/learnings` extracts the *durable insights* and writes them as notes. Both can be useful at session end, but a handoff is **never** a substitute for a learnings note (see the constraint above).

## 5. Report

One line per learning: what was captured and which layer it went to (note / case / KV). Insights must show as notes. If nothing was durable enough to keep, say that plainly.
