# Review proposals in the maintenance cockpit

The maintenance ledger collects findings the linter is unsure how to act
on: stale facts, near-duplicates, semantic contradictions, mental models
that drifted off into orphan territory. The cockpit is the surface where
a human reviews those findings, picks a canned remediation (or supplies
a free-form one), attaches a justification, and — if needed — undoes
the last applied action.

The cockpit launches as a TUI on top of your terminal. It uses
[Textual](https://textual.textualize.io/) for layout and rendering;
nothing about it leaves the terminal.

## Launch the cockpit

```bash
memex lint review --vault my-vault
```

You will see a two-pane view:

- **Left** — the queue of pending proposals. LLM-flagged ones sort
  first, then rule-based ones, with newer findings on top within each
  tier.
- **Right** — the detail card for the highlighted proposal: target
  text, the rule's explanation, related units (when the rule cites
  any), and a numbered list of canned remediation options.

Move through the queue with `j` / `k` or the arrow keys. The detail
pane redraws as the highlight changes.

## Pick a remediation

Each canned option is numbered. Hit `1` to apply the recommended
choice; the ★ marker shows which one the rule's authors think is the
sensible default. The cockpit pops a small modal asking whether you
want to attach a reviewer note — `Ctrl+S` saves it, `Esc` skips it —
and then runs the action against the server.

If the action is reversible, the option line tells you so. Reversible
actions store a `prior_state` snapshot under
`evidence.resolution.followup`, which the `[R]` keybinding (covered
below) uses when you decide a verdict was premature.

The receipt prints under the detail card: `deprioritize_unit →
resolved. Refreshing queue…`. The queue then refetches, the resolved
proposal drops out, and the highlight settles on the next pending
one.

## Other — when the menu does not fit

Sometimes the canned options do not describe what you want. Press `o`
to open the **Other** modal. It does two things:

1. Asks you for a free-form description of what you would like to
   happen ("rewrite the cluster claim to mention radxa-dragon-q6a").
2. Shows you the full action catalogue filtered to actions that apply
   to this target type, and asks you to pick one as the mapping.

The free-form text rides into the resolve payload as both
`evidence.resolution.note` (the audit trail) and `params.reason` (so
actions that accept a `reason` field — e.g. `deprioritize_unit` —
have human context to log). The mapped canned action is what actually
mutates state; the text never executes anything on its own. That is
the explicit guarantee: a free-form Other never fires a mutation the
system does not also recognise as one of its own canned actions.

If none of the canned actions fits, map to `no_op` — the verdict and
note are recorded, status flips to `resolved`, and no state changes.

## Stage a note before resolving

Press `n` to type a reviewer note before you decide. The cockpit holds
the note in the local effect line so you have it on screen while you
think. When you commit a verdict (`1`–`9`, `o`), the note is sent to
the server as part of the resolve payload and stored under
`evidence.resolution.note`.

In this release the staged note is in-session only — quitting the
cockpit drops anything you have not committed. Persisting staged
notes across sessions (so a returning reviewer sees their past
reasoning on a pending proposal) ships in a follow-on PR; the
`evidence.staged_notes` array referenced in the plan is not yet
written.

## Reverse a previous verdict

Press `r` to open the reverse modal. Paste the finding id of a
resolved proposal — the cockpit checks whether the action was
reversible, calls the action's `reverse()` with the captured
`prior_state`, and writes a reversal audit row alongside the original
resolution.

Forward-only actions cannot be reversed. The cockpit shows them with a
warning badge on the option line; if you try to reverse one, the
server returns 409 with a `forward_only` marker and the cockpit
renders that verbatim. **No forward-only action is registered in this
release** — the registry shipped only reversible actions
(`no_op`, `deprioritize_unit`, `restore_unit`, `archive_mental_model`).
The forward-only path is wired so future actions like
`regenerate_mental_model` can plug in without further server work.

## Keybindings cheat-sheet

| Key | Action |
|-----|--------|
| `j` / `k` / arrows | Navigate the queue |
| `1`–`9` | Pick a numbered option from the detail card |
| `o` | Other — free-form description mapped to a canned action |
| `n` | Add a reviewer note (proposal stays pending) |
| `r` | Reverse a resolved proposal |
| `F5` | Refresh the queue from the server |
| `?` | Help |
| `q` | Quit |

## Headless / CI fallback

`memex lint review --no-tui` falls back to the legacy prompt-loop
reviewer used in `memex lint review` before the cockpit landed. The
prompt loop reads keypresses from stdin, so it works in environments
that cannot host a Textual app (CI runners, scripted teardown, SSH
sessions without a real terminal). Pair it with `--apply` to commit
verdicts; without `--apply` the loop runs as a dry-run preview.

## Attended-mode gate

Resolve (with an action) and reverse are destructive: the cockpit
calls the registry's `execute()` and `reverse()` methods, which mutate
memory units, mental models, and link types in your vault. When you
run Memex with `server.auth.enabled=false` (the local-dev default),
**these two endpoints** refuse to commit unless
`MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1` is in the environment. The
cockpit surfaces the server's 403 message as the effect line on the
detail pane.

Dismiss is not gated — it flips status to `dismissed` and stores the
note, but never mutates the underlying memory unit / model. So `[D]`
verdicts go through cleanly even without the env var.

This guards against an unattended driver — a forgotten script, a
runaway LLM — driving the cockpit end-to-end and shipping mutations
that nobody reviewed. Pass the env var only when you know the
attended path runs from CI under human supervision.

## The live action catalogue

On its first fetch the cockpit swaps its built-in option list for the
server's live catalogue (`GET /api/v1/lint/actions`), so new server-side
actions appear in the menus without a CLI upgrade. Offline it degrades
to the built-in list.

When an external proposal carries a `proposed_action`, the cockpit
surfaces it first as **"(suggested by submitter)"** with the submitter's
params prefilled — it is advisory; every other option stays available.

## Merging an entity cluster into a NEW entity

In collapse mode (entity-cluster findings), `[a]` merges the selected
members into the chosen winner — and `[n]` merges them into a **new**
entity instead: select members with `Space` as usual, press `[n]`, type
the new canonical name, and `Enter`. All selected members fold onto the
freshly created entity (links, aliases, counters, mental models) and are
hard-deleted. Like the winner merge, this is NOT reversible.

## Irreversible actions and previews

Forward-only actions (the entity merges, `kv_delete`, `record_outcome`,
`delete_note`, `delete_entity`, `delete_mental_model`) cannot be undone
with `[r]` — the server refuses with `409 forward_only`. The prompt-loop
reviewer (`--no-tui`) fetches a live blast-radius preview
(`POST /lint/findings/{id}/preview`) and asks for explicit confirmation
before executing any of them; prefer the lifecycle alternatives
(`set_note_status`, `archive_mental_model`, `deprioritize_unit`) unless
the content must actually go away.

## What the cockpit does NOT do

It does not change the way the linter generates findings. Lint
proposals come from the SQL rules and the LLM check pass on the
scheduler's six-hour tick — and from **external submitters** (agent
skills posting to `/api/v1/lint/proposals`, shown with
`source=external`). The cockpit only handles the human-review side of
the loop: picking, reasoning, executing, and undoing.

It does not auto-resolve anything. Even on `no_op`, you press a key
that means "I have read this and have decided not to act." That is by
design — the cockpit is the high-level control surface, not the
automation.
