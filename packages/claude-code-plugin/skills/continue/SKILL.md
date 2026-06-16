---
name: continue
description: "Resume work from previous /handoff notes. Presents a multi-select list of the most recent handoff summaries, lets the user pick any number of relevant items (or 'load more'), then loads every selected note, summarizes them jointly, and asks what the next steps should be. Use at the start of a fresh session, or when the user says 'pick up where we left off', 'continue', 'what was I working on', 'resume', or 'catch me up on this'."
argument-hint: "[optional: project or topic to resume]"
---

# /continue — Resume from handoff summaries

You have been invoked via the `/continue` slash command. This is the companion to `/handoff`: it reads back the technical handoff summaries that `/handoff` writes, so the user can resume a thread across sessions.

Handoffs are notes carrying the `handoff` tag plus the plugin's auto-injected `project:*` tag. `/continue` is a **browse-then-load** flow: present a recency-ordered **multi-select** list, let the user choose **one or more** handoffs, then load and summarize every selected note together and jointly decide next steps.

## 1. Resolve scope

Find the current project's tag (`project:<id>`) in the auto-injected metadata block of the SessionStart context — the same tag `/handoff` stamped onto its notes. If `$ARGUMENTS` names a different project or a topic, use it to narrow. If you can't resolve a project tag, fall back to all `handoff`-tagged notes.

## 2. List the handoffs

```text
memex_list_notes(tags=["handoff", "project:<id>"], date_by="created_at", limit=5, slim=False)
```

`slim=False` keeps each note's `title`, `description`, and timestamp. If the project-scoped list is empty, retry with `tags=["handoff"]` so a first-time-in-this-repo `/continue` still finds cross-project handoffs. If there are none at all, say so plainly and stop — suggest `/handoff` to start leaving them.

Present the results as a **multi-select** question using `AskUserQuestion`. The user must be able to select several notes at once. Set the question's `multiSelect` flag to `true` — `kind: "multi_select"` alone is not enough:

```json
{
  "header": "Recent handoff notes",
  "question": "Which handoff(s) do you want to continue?",
  "kind": "multi_select",
  "multiSelect": true,
  "options": [
    {
      "label": "1. <short note title>",
      "description": "<status / next step one-liner>",
      "preview": "<note description + status + next step>\n\n<date>",
      "value": "<note_id>"
    },
    ...
  ]
}
```

Guidelines for the options:

- `label` — short numeric identifier plus the handoff note **title** (e.g., `1. /handoff and /continue skills`). The title must be visible in the list without selecting the item.
- `description` — a concise status or next-step signal for the handoff.
- `preview` — the note `description` plus any obvious status/next-step signal, followed by the date reference. Keep it concise so the side-by-side layout stays readable.
- `value` — the note `id` returned by `memex_list_notes`.

Always append one extra option:

```json
{
  "label": "more",
  "description": "Load more handoffs",
  "value": "more"
}
```

Do not dump the list as plain text or ask the user to type numbers manually. The selection UI is the required presentation.

## 3. Handle the user's choice

`AskUserQuestion` with `multi_select` returns a list of selected `value`s.

- **One or more note IDs** — read **every** selected handoff, not just the first one. If a note is over ~500 tokens, paginate via `memex_get_page_indices` + `memex_get_nodes` rather than `memex_read_note` (which errors above that cap).
- **`more` selected** — call `memex_list_notes` again with the same tags and a larger `limit` (e.g., 10) and re-present the extended list as a fresh `AskUserQuestion`. Do not read any note yet; wait for a selection. If `more` is selected together with note IDs, treat it as "load more first" — ignore the IDs and reload.
- **Nothing selected** — repeat the same `AskUserQuestion` once. If the user still selects nothing, stop and ask whether to broaden the scope or create a fresh `/handoff`.
- **No relevant handoffs** — stop and ask whether to broaden the scope or create a fresh `/handoff`.
- **Ambiguous or no reply** — repeat the `AskUserQuestion`. Do not guess which handoff to load.

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
