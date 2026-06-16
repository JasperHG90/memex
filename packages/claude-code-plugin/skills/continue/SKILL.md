---
name: continue
description: "Resume work from previous /handoff notes. Lists the most recent handoff summaries, lets the user pick which are relevant (one, many, or 'load more'), then loads only the selected notes, summarizes them, and asks what the next steps should be. Use at the start of a fresh session, or when the user says 'pick up where we left off', 'continue', 'what was I working on', 'resume', or 'catch me up on this'."
argument-hint: "[optional: project or topic to resume]"
---

# /continue — Resume from handoff summaries

You have been invoked via the `/continue` slash command. This is the companion to `/handoff`: it reads back the technical handoff summaries that `/handoff` writes, so the user can resume a thread across sessions.

Handoffs are notes carrying the `handoff` tag plus the plugin's auto-injected `project:*` tag. `/continue` is a **browse-then-load** flow: present a recency-ordered menu, let the user choose, then summarize only what they selected and jointly decide next steps.

## 1. Resolve scope

Find the current project's tag (`project:<id>`) in the auto-injected metadata block of the SessionStart context — the same tag `/handoff` stamped onto its notes. If `$ARGUMENTS` names a different project or a topic, use it to narrow. If you can't resolve a project tag, fall back to all `handoff`-tagged notes.

## 2. List the handoffs

```text
memex_list_notes(tags=["handoff", "project:<id>"], date_by="created_at", limit=5, slim=False)
```

`slim=False` keeps each note's `title`, `description`, and timestamp. If the project-scoped list is empty, retry with `tags=["handoff"]` so a first-time-in-this-repo `/continue` still finds cross-project handoffs. If there are none at all, say so plainly and stop — suggest `/handoff` to start leaving them.

Present the results as a numbered list with:

- the **title**,
- the **description**,
- a **date** reference.

Then ask the user to pick:

> Which handoff(s) are relevant? Reply with the number(s) (e.g. `1`, `2, 4`, `all`), or say `more` to load the next batch.

## 3. Handle the user's choice

- **Numbers / `all`** — read each selected handoff. If a note is over ~500 tokens, paginate via `memex_get_page_indices` + `memex_get_nodes` rather than `memex_read_note` (which errors above that cap).
- **`more` / `next`** — call `memex_list_notes` again with the same tags and a larger `limit` (e.g. 10) and re-present the extended list. Do not read any note yet; wait for a selection.
- **No relevant handoffs** — stop and ask whether to broaden the scope or create a fresh `/handoff`.
- **Ambiguous or no reply** — repeat the list and the prompt. Do not guess which handoff to load.

## 4. Summarize the selected handoffs

Once the user has selected one or more handoffs, read them and produce a concise combined summary:

- What each piece of work was about.
- Where each stands.
- The threads that connect or conflict across them.
- The open items that need carrying forward.

Keep citations: mention the handoff note titles so the user knows which summaries fed the brief.

## 5. Ask for next steps

Do not assume what to do next. After the summary, explicitly ask:

> Given the above, what do you want to do next? Continue one of these threads, start something new, or refine the summary?

Wait for the user's direction before taking further action.
