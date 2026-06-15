---
name: case
description: "Deliberately capture what you JUST did as a Memex case — a worked episode (trigger / situation / actions / outcome / lesson) the system derives a reusable procedure from. Use right after finishing a multi-step task, diagnosing a bug, or resolving an incident, or when asked to 'record/log/save how I did this'. Distinct from /remember (auto-routes by shape) and /extract-case (converts existing content) — this is the structured, in-the-moment path."
argument-hint: "[what you just did, e.g. 'fixed the staging deploy timeout']"
---

# /case — Capture a worked episode now

A **case** is a reusable worked episode: a concrete task, the steps you took, and how it turned out. Cases feed Memex's procedural plane — the system **derives** the procedure (and any higher-order strategy) from the cases you file. You never author a procedure directly.

This skill is for capturing an episode **as you go** — "save how I just did this". It differs from its siblings:
- `/extract-case` converts *existing* content (a note, file, or URL) into a case.
- `/remember` auto-routes by shape and may pick a case, a note, or KV.
- `/case` is the deliberate, structured "file this episode now" path — it guarantees the clean template the derivation wants.

## 1. Source the episode

Use `$ARGUMENTS` and the recent conversation. If there's no clear worked episode (no concrete task with steps and an outcome), say so and stop — suggest `/remember` for a fact/decision instead. Don't manufacture a case out of an unfinished plan or an opinion.

## 2. Compose the fields

Ground every field in what actually happened — don't invent steps:
- **title**: the task in a few words.
- **trigger**: what kicked it off / the symptom (required).
- **situation**: the context, one or two sentences.
- **actions**: the ordered steps actually taken, service-agnostic where possible.
- **outcome**: `"success"` | `"failure"` | `"mixed"` (required).
- **lesson**: the one takeaway worth carrying forward.

## 3. Probe for an existing procedure (preferred)

Link the case to a procedure the system already derived, so it lands in the right cluster:

```text
# (scope, verb, context) — e.g. verb="deploy", context="staging"
memex_procedural_get_by_identity(kind="procedure", scope="<scope>", verb="<verb>", context="<context>")
```

Pick `scope` by where the how-to belongs: `project:<id>` for a project-specific workflow, `app:<id>` for an app-specific one, `global` for a cross-project convention. If a project/app procedure is plausible, probe that scope before falling back to `global` — `get_by_identity` is an exact-anchor lookup, so the wrong scope misses. If every probe returns `null`, optionally try `memex_procedural_search(query="...", kind="procedure")` to spot a near-anchor before giving up on linking. If you find one, pass its id as `case_of` below; otherwise leave `case_of` unset.

## 4. File the case

```text
memex_case_submit(
  title=..., trigger=..., situation=..., actions=[...],
  outcome="success|failure|mixed", lesson=...,
  case_of=<id-if-found>,
)
```

Do **NOT** also `memex_add_note` the same episode — a how-to saved as a note is invisible to the procedural plane. The plugin auto-injects ambient tags (surface / session / project / git / model / plugin); don't set them yourself.

## 5. Report the assignment outcome

`memex_case_submit` returns an `assignment_mode`. Tell the user which one came back:
- `queued` — the DEFAULT under Claude Code: the case was filed in the background (non-blocking) and the assignment resolves async. You get a `job_id`, not an inline outcome; any escalation / new-procedure draft surfaces later in the lint queue. Report that it's filed + queued.
- `explicit` — linked to the `case_of` procedure you passed.
- `auto_assigned` — the server matched it to an existing procedure with a clean signal.
- `new_procedure_draft` — it seeded a new draft procedure (awaits operator confirmation).
- `escalated` — the assignment was contested; it's queued in the lint queue (`finding_id`) for human review.
- `skipped` — assignment is disabled on this server, or there was nothing to assign; the case is still filed.

The non-`queued` modes (with inline `note_id` + assignment outcome) come back only when you pass `background=false` to wait synchronously — do that when the user needs the assignment result in this turn.

State the case (title + outcome) and what happened to it in one or two lines.
