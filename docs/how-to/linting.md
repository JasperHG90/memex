# Review and apply lint proposals

Memex runs a background maintenance linter that scans the vault for
stale facts, near-duplicates, contradicted units, dangling entity refs,
and other hygiene issues. The linter never edits memory on its own. It
records findings — small advisory rows describing what it noticed —
and waits for you to act on them.

This guide walks the loop you run on a quiet morning: list what is
pending, inspect a finding, accept an LLM-proposed contradiction
winner, reverse one if the apply was premature, and dismiss the ones
that do not warrant action.

The MCP tools `memex_get_lint_flags`, `memex_lint_apply_winner`, and
`memex_lint_reverse_winner` cover the same surface; agent-driven
workflows use those. The shapes match the CLI subcommands documented
below.

## Prerequisites

- A vault that has been ingested long enough for the lint pass to have
  run at least once (the scheduler ticks every six hours by default).
- The LLM-gated lint pass enabled if you want winner proposals on
  contradictions: `server.memory.lint_llm.enabled=true` and
  `server.memory.lint_llm.checks.propose_contradiction_winner.enabled=true`.
- Authentication that grants write access to the vault. Apply and
  reverse are write operations; list and inspect are read-only.

## Procedure

### 1. List pending findings

Start with the per-scope summary:

```bash
memex lint status --vault my-vault
```

The output names the vault and prints a pending count. Use
`--global` for vault-NULL findings (entity-collapse clusters live
there), or `--all` for the total across every scope (the default).

For the actual list, run `findings`:

```bash
memex lint findings --vault my-vault --status pending --limit 50
```

You will see a table with one row per finding. The columns are
`id` (truncated for display), `lint_type` (one of `structural`,
`quality`, `governance`, `schema`), `rule_name`, `target_type`,
`target_id`, and `vault_id`.

Filter to a single lint type with `--type quality` when the table is
long. For winner proposals specifically, filter by rule name once you
inspect the full row (the table itself does not filter by rule).

If you are driving Memex through MCP, call `memex_get_lint_flags`
with the same shape: `vault_id`, `lint_type`, `status` (default
`pending`), `limit` (default 20, max 200), and an optional `cursor`
for paging. The tool binds to the session's active write vault when
`vault_id` is omitted — it does not fall through to a global view.

### 2. Inspect a finding

The `findings` table truncates fields for screen width. To see the
full row — including the `evidence` payload that names winner and
loser unit IDs, the proposed action, the LLM's confidence, and the
rationale — increase the limit and read the JSON the server
returns:

```bash
memex lint findings --vault my-vault --limit 200
```

You can also walk findings interactively (more on this in
[the dry-run loop](#using-the-interactive-review-loop)).

Each `propose_contradiction_winner` finding's `evidence` carries:

- `winner_unit_id` / `loser_unit_id` — the two units in tension.
- `action` — one of `mark_loser_stale`, `supersede_loser_note`,
  `refine_not_contradict`, `inconclusive`.
- `confidence` — the LLM's calibrated confidence in the proposal.
- `rationale` — a one- or two-sentence justification.
- `linked_to_finding` — the upstream FSFM finding that triggered
  this proposal.

Read the rationale before you apply. The linter writes proposals it
is willing to defend; you are the one who decides whether the
defence holds up against context the linter does not have.

### 3. Apply an LLM-proposed winner

When you accept a winner proposal, run:

```bash
memex lint apply <finding_id>
```

Use the full UUID, not the truncated form from the table. The action
recorded under `evidence.action` drives the mutation
<code-ref path="packages/core/src/memex_core/services/contradiction_resolution.py" lines="207-330" />:

| Action                  | Effect                                                                                                                                                                |
|-------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `mark_loser_stale`      | Sets the loser memory unit's `status` to `stale`.                                                                                                                      |
| `supersede_loser_note`  | Sets the loser note's `superseded_by` to the winner note's id. Falls back to `mark_loser_stale` when both units share a parent note; the fallback reason is recorded. |
| `refine_not_contradict` | Rewrites the inbound `MemoryLink.link_type` from `contradicts` to `refines` (graph-pressure weight `0.0`).                                                              |
| `inconclusive`          | No-op write; flips the finding to `resolved`.                                                                                                                          |

The apply path captures the affected row's pre-mutation values
under `evidence.resolution.prior_state` so the change is fully
reversible. The finding flips from `pending` to `resolved` in the
same transaction.

Output names the effective action and any fallback reason:

```
applied: 7c8e... (action=mark_loser_stale)
```

For non-winner-proposal findings, do not use `apply`. Use
`memex lint resolve <finding_id>` to mark the finding handled
without a mutation, or `memex lint dismiss <finding_id>` to discard.
The `apply` subcommand only knows how to dispatch
`propose_contradiction_winner` findings.

### 4. Reverse an applied winner

If you applied a proposal you no longer want — say the loser unit
turns out to be correct after all, or you applied during triage and
want to undo before the next ingest — reverse it:

```bash
memex lint reverse <finding_id>
```

The reverse path reads `evidence.resolution.prior_state` and
atomically restores the affected memory unit, note, or memory link
<code-ref path="packages/core/src/memex_core/services/contradiction_resolution.py" lines="447-540" />.
It also writes a paired audit row
(`rule_name=propose_contradiction_winner_reversal`) so the audit
trail shows both the apply and the reverse. The original finding
stays in `resolved` status — flipping it back to `pending` would
violate the partial unique index on pending findings.

### 5. Dismiss a finding without acting

When the linter raises a finding you do not want to act on — a
near-duplicate that is intentional, a contradiction that reflects a
real change in the world you have already captured elsewhere —
dismiss it:

```bash
memex lint dismiss <finding_id>
```

The finding flips from `pending` to `dismissed` and stops showing
up in the default listings. Pass `--status dismissed` to
`memex lint findings` if you want to audit what you have set aside.

### Using the interactive review loop

When the pending backlog is more than a handful, walk it
interactively:

```bash
memex lint review --vault my-vault --apply
```

The loop renders each finding as a card and prompts for a single
keystroke: `a` accepts (resolves), `d` dismisses, `s` skips, `q`
quits. Without `--apply` the loop is a dry-run preview — verdicts
are collected and summarised, but nothing is written. Use the
dry-run first if you want to scope the work before committing.

The review loop sends accepted findings through `lint_resolve` (not
`lint_apply`), so it does not run winner-proposal mutations. To
apply winner proposals, run `memex lint apply <finding_id>`
directly after you have triaged with `review`.

## Verification

After an apply, confirm the finding moved out of `pending`:

```bash
memex lint findings --vault my-vault --status resolved --limit 10
```

The row you applied appears in the resolved list. The mutation it
recorded — a stale loser, a superseded note, a rewritten link — is
visible through the usual surfaces: `memex_memory_search`,
`memex_get_memory_units`, or `memex_get_memory_links` against the
target IDs from the original finding.

After a reverse, confirm a paired
`propose_contradiction_winner_reversal` row exists in the findings
listing and the original row still shows `status=resolved`.

## Troubleshooting

**Apply succeeds but the mutation looks like a no-op.** The LLM was
not confident enough. Definitive verdicts with
`confidence < server.memory.lint_llm.propose_winner_min_confidence`
(default `0.6`) are emitted with `action='inconclusive'`, so the
apply path runs a no-op write and flips the finding to `resolved`
<code-ref path="packages/core/src/memex_core/memory/lint_llm/checks.py" lines="469-480" />.
Read `evidence.action` before applying; raise or lower the
threshold via `MEMEX_SERVER_LINT_LLM_PROPOSE_WINNER_MIN_CONFIDENCE`
to change the cut-off.

**Reverse returns HTTP 409 "current state has diverged from the
applied state".** Another writer changed the affected row after you
applied. The reverse path refuses to clobber unrelated edits
<code-ref path="packages/core/src/memex_core/services/contradiction_resolution.py" lines="459-464" />.
Inspect the current state of the loser unit, note, or link;
reconcile by hand if the divergence is real, or retry the reverse
if the divergence was transient.

**Apply returns "is not a propose_contradiction_winner finding".**
You ran `memex lint apply` against a finding from a different rule
(an FSFM composite, an entity-collapse cluster, a duplicate
cluster). The apply subcommand only dispatches winner proposals.
Use `memex lint resolve <finding_id>` for non-winner findings;
`entity_collapse_cluster` rows take the `--winner / -w` override on
`resolve`.

**Apply returns "Concurrent modification — retry the request".**
The CAS guard on the loser row caught a race against another writer
between the load and the update. Re-run the apply — the second
attempt reads the current state and either applies cleanly or
surfaces a real divergence.

## See also

- [How-to: Deprioritize memory units](deprioritize-units.md)
- [Reference: lint API](../reference/lint-api.md)
- [Explanation: how lint findings flow through the ledger](../explanation/maintenance-linter.md)
- [Tutorial: First-time vault setup](../tutorials/getting-started.md)
