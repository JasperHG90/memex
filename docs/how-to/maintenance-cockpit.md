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

## Where routing proposals come from

Inbox routing is no longer a built-in engine. The `triage-inbox` workspace
skill classifies notes sitting in the `inbox` vault and files
`inbox_vault_route` (or `inbox_vault_no_fit`) proposals through the
external-lint ingress; they surface in this cockpit like any other finding.
Accepting a route runs the reversible `route_note_to_vault` action, which
migrates the note — plus its units, chunks, and links — to the chosen vault.

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

## Filter the queue by rule and bulk-clear

When one rule is flooding the queue (a noisy `llm_schema_drift` or
`llm_semantic_contradiction` run, say), narrow the view to it instead of
scrolling past everything else. Press `/` to open the rule picker: it
lists every rule present in the loaded queue with a count, plus an
**All rules (N)** entry at the top. Pick a rule and the queue collapses
to just those findings; the header switches from `Pending (N)` to
`Pending (shown/loaded) · <rule>`. Pick **All rules** (or press `Esc` in
the picker) to clear the filter.

The filter is the front half of a bulk-clear. With a rule selected,
press `a` to select every finding in the filtered view, then resolve or
dismiss the selection in one batch (`Enter` opens the batch action menu;
`dismiss` flips them all to `dismissed` in a single submit). This is the
fastest way to drain a rule you have judged not worth acting on — filter
to it, `a`, dismiss.

Two honest limits. The filter runs over the findings **loaded** into the
cockpit, so the counts read "loaded", not the true vault-wide total; the
queue loads up to `--limit` rows (default 200, max 500). And changing the
filter rebuilds the queue, which clears any multi-select — select, then
act, before switching rules.

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

The status bar at the bottom always shows the keys live for the current
mode. The full map:

| Mode | Key | Action |
|------|-----|--------|
| LIST | `↑` / `↓` | Navigate the queue |
| LIST | `Enter` | Open the highlighted finding in REVIEW |
| LIST | `d` | Drill into unit DETAIL |
| LIST | `Space` | Toggle multi-select; `Shift+↑/↓` selects and moves |
| LIST | `a` | Select all findings in the current (filtered) view |
| LIST | `Esc` | Deselect all |
| LIST | `/` | Filter the queue by rule (pick a rule, or "All rules" to clear) |
| LIST | `f` | Toggle the flag bookmark on the highlighted finding |
| REVIEW | `↑` / `↓` | Navigate the action list |
| REVIEW | `Enter` | Confirm the action (opens the note area) |
| REVIEW | `n` | Toggle the reviewer-note area |
| DETAIL | `Tab` / `↑` / `↓` | Cycle between units in the finding |
| DETAIL | `s` | View the source note text |
| COLLAPSE | `Space` `w` `a` `n` `x` | In/out · winner · apply · new entity · dismiss |
| any | `Esc` | Back to the previous mode |
| any | `r` | Reverse a resolved proposal (paste its finding_id) |
| any | `F5` | Refresh the queue from the server |
| any | `?` | Help |
| any | `q` | Quit |

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
with `[r]` — the server refuses with `409 forward_only`. Both review
surfaces fetch a live blast-radius preview
(`POST /lint/findings/{id}/preview`) and demand explicit confirmation
before executing one: the TUI opens a `[y]/[n]` modal with the preview
text; the prompt-loop reviewer (`--no-tui`) prints it and asks. Batch
accept never executes an irreversible action — those findings are
skipped with "review singly". Prefer the lifecycle alternatives
(`set_note_status`, `archive_mental_model`, `deprioritize_unit`) unless
the content must actually go away.

## What the cockpit does NOT do

It does not change the way the linter generates findings. Lint
proposals come from the SQL rules and the LLM check pass on the
scheduler's six-hour tick — and from **external submitters** (agent
skills posting to `/api/v1/lint/proposals`, shown with
`source=external`). The cockpit only handles the human-review side of
the loop: picking, reasoning, executing, and undoing.

To keep the LLM check pass from flooding the queue, its per-tick
candidate selection applies two guards: it audits **at most one memory
unit per source note per tick** (so a multi-unit note — an RFC, a
post-mortem — cannot emit a burst of near-identical findings at once),
and it skips units the extractor labelled `intent_class='ephemeral'`
(short-lived facts whose content decays). This narrows volume, not
coverage of distinct notes; a sibling unit on the same note is eligible
on a later tick. To cut volume further, raise
`server.memory.lint_llm.surprise_threshold`, set
`server.memory.lint_llm.confidence_gate.confidence_min`, or disable a
noisy check outright with
`server.memory.lint_llm.checks.<check>.enabled=false` — see the
[configuration reference](../reference/configuration-options.md#lintllmconfig).

It does not auto-resolve anything. Even on `no_op`, you press a key
that means "I have read this and have decided not to act." That is by
design — the cockpit is the high-level control surface, not the
automation.
