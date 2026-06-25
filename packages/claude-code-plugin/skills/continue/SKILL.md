---
name: continue
description: "Resume work from previous /handoff notes. Presents a multi-select list of the most recent handoff summaries, lets the user pick any number of relevant items (or 'load more'), then loads every selected note, summarizes them jointly, and asks what the next steps should be. Use at the start of a fresh session, or when the user says 'pick up where we left off', 'continue', 'what was I working on', 'resume', or 'catch me up on this'."
argument-hint: "[optional: project or topic to resume]"
---

# /continue — Resume from handoff summaries

You have been invoked via the `/continue` slash command. This is the companion to `/handoff`: it reads back the technical handoff summaries that `/handoff` writes, so the user can resume a thread across sessions.

Handoffs are notes carrying the `handoff` tag plus the plugin's auto-injected `project:*` tag. `/continue` is a **browse-then-load** flow: present a recency-ordered **multi-select** list, let the user choose **one or more** handoffs, then load and summarize every selected note together and jointly decide next steps.

## 1. Resolve scope — vault first, then project tag

`/handoff` writes its notes into the **project's vault** (the `PreToolUse` hook stamps `vault_id` automatically). So `/continue` MUST list from that same vault — listing without a `vault_id` queries the server's default read scope, which is usually NOT the project vault, and returns nothing even when the briefing shows the vault has notes. This is the #1 cause of "no handoffs found".

Read two things from the SessionStart context:
- **The active project vault** — the "Per-project vault" block (`This project uses vault \`<vault>\``). This is the `vault_id` to scope to.
- **The project tag** — the `project:<id>` tag in the auto-injected metadata block, the same tag `/handoff` stamps on each note.

<critical_constraint name="continue_vault_scope">
Vault-scope rule for EVERY `memex_list_notes` call in this skill:
- **A project vault is set → pass `vault_id="<that vault>"`.** Never omit it when a project vault is known.
- **No project vault is set → omit `vault_id`** (the server's default / global read scope).
- **The user explicitly asks for every project's handoffs (all vaults) → pass `vault_id="*"`** (the wildcard expands to all content vaults). Use `"*"` only on explicit request, or as the final broaden step in §2 — never as the default.
</critical_constraint>

If `$ARGUMENTS` names a different project or topic, use it to narrow the tag filter (and the vault, if it names one).

## 2. List the handoffs

```text
memex_list_notes(vault_id="<project vault>", tags=["handoff", "project:<id>"], date_by="created_at", limit=4, slim=False)
```

`slim=False` keeps each note's `title`, `description`, `timestamp`, and extracted `summaries` (`topic` + `key_points`) — these key_points are rich enough to summarize from, so do NOT auto-fetch full note bodies here (see §4 for when to escalate).

If that list is empty, broaden **in order** (per the scope rule above):
1. Drop the project tag — `memex_list_notes(vault_id="<project vault>", tags=["handoff"], …)` — in case the handoffs in this vault carry a different `project:*` tag.
2. Only if still empty, widen the vault — `memex_list_notes(vault_id="*", tags=["handoff"], …)` — so a first-time-in-this-repo `/continue` still finds cross-project handoffs.

If there are none at all, say so plainly and stop — suggest `/handoff` to start leaving them.

Present the results as a **multi-select** question using `AskUserQuestion`. The user must be able to select several notes at once. Set the question's `multiSelect` flag to `true` — `kind: "multi_select"` alone is not enough.

**`AskUserQuestion` caps a single question at 4 options total.** Reserve one slot for the `more` affordance, so present **the 3 most recent handoffs + `more`** — never attempt to show more than 3 real notes in one question.

```json
{
  "header": "Recent handoffs",
  "question": "Which handoff(s) do you want to continue? (pick any number)",
  "multiSelect": true,
  "options": [
    {
      "label": "1. <short note title>",
      "description": "<status / next step one-liner>",
      "preview": "<from summaries[].topic + key_points: what it's about + where it stands>\n\n<date>",
      "value": "<note_id>"
    },
    {
      "label": "2. <short note title>",
      "description": "<status / next step one-liner>",
      "preview": "<from summaries[].topic + key_points: what it's about + where it stands>\n\n<date>",
      "value": "<note_id>"
    },
    {
      "label": "3. <short note title>",
      "description": "<status / next step one-liner>",
      "preview": "<from summaries[].topic + key_points: what it's about + where it stands>\n\n<date>",
      "value": "<note_id>"
    },
    {
      "label": "more",
      "description": "Load more handoffs",
      "value": "more"
    }
  ]
}
```

Guidelines for the options:

- `label` — short numeric identifier plus the handoff note **title** (e.g., `1. /handoff and /continue skills`). The title must be visible in the list without selecting the item.
- `description` — a concise status or next-step signal, composed from the note's `summaries[].topic`/`key_points`.
- `preview` — built from the note's `summaries` (`topic` + `key_points`) followed by the date reference. NOTE: `memex_list_notes` returns `title`, `created_at`, and `summaries` — there is **no** note `description` field on the result, so compose previews from `summaries`, never from a (nonexistent) `description`. Keep it concise so the side-by-side layout stays readable.
- `value` — the note `id` returned by `memex_list_notes`.

Do not dump the list as plain text or ask the user to type numbers manually. The selection UI is the required presentation.

## 3. Handle the user's choice

`AskUserQuestion` with `multiSelect: true` returns a list of selected `value`s.

- **One or more note IDs** — proceed to §4 with the selected notes. Do NOT blindly deep-fetch every selected note; load surgically (see §4).
- **`more` selected** — re-call `memex_list_notes` with the **same `vault_id` and tags** (per the §1 scope rule) and a larger `limit` (e.g. `limit=10`). The tool exposes no `offset` parameter, so it will return the same first notes again — **dedup client-side against the note IDs you have already shown** and present only the next 3 unseen as a fresh `AskUserQuestion` (still 3 notes + `more`). Do not read any note yet; wait for a selection. If `more` is selected together with note IDs, treat it as "load more first" — ignore the IDs and reload.
- **Nothing selected** — repeat the same `AskUserQuestion` once. If the user still selects nothing, stop and ask whether to broaden the scope or create a fresh `/handoff`.
- **No relevant handoffs** — stop and ask whether to broaden the scope or create a fresh `/handoff`.
- **Ambiguous or no reply** — repeat the `AskUserQuestion`. Do not guess which handoff to load.

## 4. Summarize the selected handoffs

Load **surgically**, in tiers — do not auto-fetch full note bodies. The default brief is built from what `memex_list_notes(slim=False)` already returned (each note's `topic` + `key_points`), escalating to a full fetch only when needed.

**Tier 0 — list output (default):** the `summaries[].topic` and `summaries[].key_points` from the §2 `list_notes` call are usually enough to state what each handoff is about, where it stands, and the open items. Summarize directly from them. No extra tool call.

**Tier 1 — surgical section fetch (escalate when Tier 0 is thin):** if a selected note's `key_points` are too sparse to state what it is / where it stands / the next step, escalate **that note only**. Call `memex_get_page_indices(note_ids=[...], depth=1)` once for all notes being escalated (batched), then `memex_get_nodes(node_ids=[...])` for **only the `Summary` and `Next steps` node IDs** (batched). Skip `Where it stands` (Tier 0 already covers it) and `Key references` (citation-only). Do not fetch every section of every note — pick the load-bearing sections per note.

**Tier 2 — full fetch (on demand):** only when the user asks to act on a specific file/commit/reference. Fetch the `Key references` (or remaining) nodes for that note then.

Why tiered: the two-step `get_page_indices` → `get_nodes` protocol is inherent to the paginate-over-500 design and can't be collapsed, so defaulting to Tier 0 keeps the common path to `list → select → summarize` and reserves the deep fetch for when it earns its keep.

Produce a concise combined summary:

- What each piece of work was about.
- Where each stands.
- The threads that connect or conflict across them.
- The open items that need carrying forward.

Keep citations: mention the handoff note titles so the user knows which summaries fed the brief.

## 5. Ask for next steps

Do not assume what to do next. After the summary, explicitly ask:

> Given the above, what do you want to do next? Continue one of these threads, start something new, or refine the summary?

Wait for the user's direction before taking further action.
