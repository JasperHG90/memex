---
name: handoff
description: "Write a technical handoff summary of the current work to Memex — a descriptive, summarizing writeup of what you've been doing, the approach and key decisions, where it stands, and what's next — so you (or another agent) can pick the thread back up later. Use at the end of a workday, when wrapping up an involved conversation, or whenever the user says 'hand this off', 'write up where we are', 'summarize this so I can continue later', or 'I'll come back to this'."
argument-hint: "[optional: focus / what to hand off]"
---

# /handoff — Write a technical handoff summary

You have been invoked via the `/handoff` slash command.

The user has been working through something — an involved conversation, a piece of implementation, an investigation — and wants to **hand the work off to their future self** (or another agent). Your job is to write a clear **technical summary** of where the work stands, good enough that someone picking it up cold could get back up to speed and keep going. Think end-of-day brief or a thorough thread summary, not a status form. Its companion is `/continue`, which reads the latest handoff back at the start of a later session.

A handoff is a **note** (`memex_add_note`) carrying a single `handoff` tag, so `/continue` can find it later by recency. It is not a case (it is a summary of work, not a reusable how-to procedure) and not KV (it is not a static preference).

## 1. Understand the work

Read back over the conversation (and `$ARGUMENTS` if the user pointed at a focus). You're reconstructing the substance: what the work is actually about, what's been figured out, what was decided and why, what's still open. Capture the technical reality, not a vague gloss — the value of a handoff is that it saves the next session from re-deriving everything.

## 2. Write the summary

Compose the note as a genuine technical writeup. Lead with prose — explain the work the way you'd brief a capable colleague taking over. These headings are a useful spine, but adapt them to the work rather than filling boxes:

- **Summary** — what this work is, the approach taken, and the key technical decisions (and the *why* behind them). This is the bulk of the handoff.
- **Where it stands** — what's done and working, what's in progress, what's untested or uncertain.
- **Next steps** — the concrete threads to pick up, specific enough to act on.
- **Key references** — the files, branches, PRs, commands, and links that anchor the work.

Then set the note fields:

- **title**: `Handoff: <short descriptor of the work>` — recognizable at a glance.
- **description**: ONE crisp sentence naming the work **and** where it stands. `/continue` shows this in its candidate list, so make it stand on its own (e.g. "Vault-routing wired through the CLI and green; the alembic migration for the new column is the next step.").
- **author**: `"claude-code"`.
- **tags**: `["handoff"]`. The plugin auto-injects ambient tags (`surface:claude-code`, `session:*`, `project:*`, `git:*`, …) — `project:*` is what scopes the handoff to this repo, so don't repeat or hand-set them.

## 3. Save

Call `memex_add_note(...)` with the fields above. The plugin defaults `background: true`, which is right — extraction runs async and `/continue` finds the note by tag immediately regardless.

## 4. Stay on one plane

A handoff summarizes *where the work is*, not *how to do a recurring task* — so do NOT also `memex_case_submit` it. If, separately, the session produced a genuinely reusable how-to worth getting back next time, mention the user can run `/case` for it.

## 5. Confirm

State the handoff note's title in one line and that `/continue` will surface it at the start of the next session.
