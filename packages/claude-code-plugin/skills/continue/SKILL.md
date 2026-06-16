---
name: continue
description: "Pick up work from a previous /handoff. Reads back the most recent handoff summary for this project, briefs you on where the work stands, and offers other recent handoffs to choose from. Use at the start of a fresh session, or when the user says 'pick up where we left off', 'continue', 'what was I working on', 'resume', or 'catch me up on this'."
argument-hint: "[optional: project or topic to resume]"
---

# /continue — Pick up from a handoff

You have been invoked via the `/continue` slash command. This is the companion to `/handoff`: it reads back the technical handoff summaries that `/handoff` writes, so the user can resume a thread across sessions.

Handoffs are notes carrying the `handoff` tag plus the plugin's auto-injected `project:*` tag. Recency *is* the relevance here — the latest handoff is almost always the one to resume — so list by recency rather than relevance-ranked search.

## 1. Resolve scope

Find the current project's tag (`project:<id>`) in the auto-injected metadata block of the SessionStart context — the same tag `/handoff` stamped onto its notes. If `$ARGUMENTS` names a different project or a topic, use it to narrow. If you can't resolve a project tag, fall back to all `handoff`-tagged notes.

## 2. List the handoffs

```text
memex_list_notes(tags=["handoff", "project:<id>"], date_by="created_at", limit=5, slim=False)
```

`slim=False` keeps each note's `description` and timestamp, which you need for the candidate list. If the project-scoped list is empty, retry with `tags=["handoff"]` so a first-time-in-this-repo `/continue` still finds cross-project handoffs. If there are none at all, say so plainly and stop — suggest `/handoff` to start leaving them.

## 3. Brief the latest

Read the most recent handoff (the first result). If it's over ~500 tokens, read it via `memex_get_page_indices` + `memex_get_nodes` rather than `memex_read_note` (which errors above that cap). Give the user a tight brief from its summary: what the work was, where it stands, and the next steps. This is the fast path — most of the time this is the handoff they want, and the goal is to get them back up to speed quickly.

## 4. Offer the other threads

Present the next ~3 handoffs as a numbered list so the user can recognize and pick a different thread (an older task, a parallel branch). For each, show:

- its **description** (the crisp one-liner `/handoff` wrote), and
- a **date** reference.

Then pause for the user to choose — continue from the latest you just briefed, or pick one of the listed handoffs. Don't narrate the contents of the listed ones; the point is for the user to recognize which thread they mean, not for you to consume it for them.

## 5. On selection

Read the chosen handoff (paginating if large, per Step 3) and resume work from its **Next steps**, carrying any open threads forward. Cite the handoff note's title so the user knows exactly which summary you're resuming from.
