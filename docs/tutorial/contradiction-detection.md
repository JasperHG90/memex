# Walk through contradiction detection

Imagine you wrote a note three weeks ago saying you deploy on Friday afternoons. Last sprint your team agreed to ban Friday-afternoon deploys, and you wrote that down too. Both notes are in the vault. Both will surface on a search for "deploy schedule". Until something reconciles them, the older one keeps showing up alongside the newer one — and the agent reading the vault has no way to tell which one still holds.

In this tutorial you cause that situation on purpose, watch Memex notice it, and then apply the LLM's proposed fix. You will see every moving part: the `contradicts` link, the lint finding, the recorded action, and the reverse path. By the end you will know what to look at when this happens in your real vault.

## Prerequisites

- A working Memex install. If you do not have one, run through [Tutorial: Getting started](getting-started.md) first.
- A running Memex server. Start one in another terminal with `memex server start`.
- A vault to work in. Create one with `memex vault create scratch` if you do not have a throwaway vault already.
- The LLM-gated lint pass enabled. Confirm `server.memory.lint_llm.enabled: true` and `server.memory.lint_llm.checks.propose_contradiction_winner.enabled: true` in your config. Without these the rule-based pass still finds the pressure, but no winner is proposed.
- An LLM provider key on the environment the server uses (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`). Extraction and the LLM lint pass both need it.

You will work in one shell. Replace `scratch` with the vault name you chose if it is different. The walkthrough takes about ten minutes end to end — most of that is waiting for the lint pass to tick.

Before you start, make sure `memex lint status` returns cleanly:

```bash
memex lint status --vault scratch
```

You should see `vault <uuid>: 0 pending findings`. If the command errors, the server is not up or your vault name is wrong. Fix that before continuing.

## Step 1: Ingest the original deploy-schedule note

Write the first note. This is the one you want the lint pass to mark as the loser later — it carries the older claim about Friday afternoons.

```bash
memex note add \
  --vault scratch \
  --title "Deploy schedule" \
  --key deploy-schedule-v1 \
  --date 2026-04-29 \
  "Our team deploys to production at 5pm every Friday. The release engineer
  stays online for an hour after to handle the rollback if anything goes
  wrong, then hands off to on-call."
```

You will see something like:

```
Adding Note
Note added: <uuid>
note_key: deploy-schedule-v1
```

Behind the scenes Memex extracts memory units from the note body. Each unit is one structured claim about the world. For this note you should get a unit roughly along the lines of "the team deploys to production at 5pm every Friday".

Wait a few seconds for extraction to finish, then check that the unit landed:

```bash
memex memory search "Friday deploy schedule" --vault scratch --compact
```

You should see one row whose text mentions the 5pm Friday deploy. The output looks like:

```
- [behavior] Our team deploys to production at 5pm every Friday...
```

If you see `No results found.`, extraction has not finished yet — wait another five seconds and try again. Extraction is asynchronous; the `note add` call returns before the chunks have been embedded and the units written.

Grab the unit id for use later:

```bash
OLD_UNIT_ID=$(memex memory search "5pm Friday deploy" \
  --vault scratch --minimal --limit 1)
echo "$OLD_UNIT_ID"
```

The `--minimal` flag prints only the unit id, which makes it easy to capture into a shell variable. You will see a UUID like `b3a1e8e0-...`.

## Step 2: Ingest the contradicting note three weeks later

Now write the second note. The `--date` flag matters here — Memex uses the recorded note date for temporal ordering and for the LLM's "which note is newer" reasoning when it proposes a winner.

```bash
memex note add \
  --vault scratch \
  --title "Deploy schedule update" \
  --key deploy-schedule-v2 \
  --date 2026-05-20 \
  "After last month's incident, the team agreed: no Friday-afternoon
  deploys. The deploy window is now Monday through Thursday, 9am to 3pm.
  Friday is reserved for on-call work and post-mortems."
```

While extraction runs on this note, the contradiction engine looks for memory units in the same vault that share entities (deploy, Friday, team) and disagree with the new ones. When it finds a match, it writes a `MemoryLink` of type `contradicts` or `weakens` between the two units. The link is the first artifact you can see.

Wait a few seconds, then move on.

## Step 3: Search for both facts and observe the `contradicts` link

Run a memory search that should surface both deploy-schedule units:

```bash
memex memory search "when does the team deploy" --vault scratch
```

Both units appear in the result table — the old "5pm Friday" claim and the new "Monday-Thursday window" claim. The retrieval layer does not pick a winner. Both are still `status='active'`. From the agent's perspective, the vault has two answers to the same question.

The `contradicts` link sits on the older unit, pointing at the newer one. To see it, inspect its links (you already captured `$OLD_UNIT_ID` in Step 1):

```bash
memex memory links "$OLD_UNIT_ID"
```

You should see a row with `Relation = contradicts` (or `weakens`, depending on how confidently the contradiction engine typed the disagreement) and `Target Unit` pointing at the newer unit's id. The output looks roughly like:

```
              Links for b3a1e8e0...
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┓
┃ Relation   ┃ Target Unit ┃ Note Title              ┃ Weight ┃ Time      ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━┩
│ contradicts│ 7f44a2c1... │ Deploy schedule update  │   1.00 │ 2026-05-20│
└────────────┴─────────────┴─────────────────────────┴────────┴───────────┘
```

The `Weight` column carries the link strength — `1.0` for an explicit contradiction-of-prior, `0.7` for a resolution-weakens, lower for default-typed claims. If you want the raw JSON instead of the table, add `--json`:

```bash
memex memory links "$OLD_UNIT_ID" --json
```

This prints the full link records, including the link's own metadata (`source_credibility`, `created_at`, link payload). Useful when you want to script against the output or feed it into another tool.

The link is the raw signal. Nothing has been demoted yet. Both units are still active; the search in this step proved that. The `contradicts` link is a *marker* — it tells the lint pass which units are under pressure, but it does not, by itself, suppress anything.

## Step 4: Wait for the lint pass and check pending findings

The lint pass runs on a schedule. It will not fire instantly — by default it ticks roughly every few minutes, looking for units under graph pressure and feeding the surprising ones into the LLM for a winner proposal. Two minutes is usually plenty; if your config sets a longer `interval_seconds`, wait that long.

While you wait, poll the pending count:

```bash
memex lint status --vault scratch
```

The first time you run it you may see `0 pending findings`. Run it again every thirty seconds. When the lint pass has fired and the LLM has proposed a winner, the count goes up:

```
vault <uuid>: 2 pending findings
```

Two is what you expect here: one `composite_deprioritize_candidate` finding from the FSFM rule (the old unit is now under pressure from the `contradicts` link), and one `propose_contradiction_winner` finding from the LLM pass (the secondary proposal that actually carries the recommended action).

If you wait several minutes and still see zero, the LLM lint pass is probably disabled. Re-check the prerequisites. You can also confirm the pass is configured by looking at the diagnostics view:

```bash
memex diagnostics findings --vault scratch
```

This prints a JSON pivot of lint findings by type, status, and source. If the `(lint_type, status, source)` pivot shows zero `quality` rows across the board, no contradiction findings have been emitted yet — keep waiting. If it shows `quality` rows but all are `resolved` or `dismissed`, you may be looking at a vault that already had its findings triaged; switch to a fresh vault.

## Step 5: List the findings and locate the winner proposal

Pull the pending findings as a table:

```bash
memex lint findings --vault scratch --type quality
```

Look for the row whose `rule_name` is `propose_contradiction_winner`. The `target_id` is the loser unit — the one the LLM proposes to demote. Copy the finding's full id; you will need it for the apply step. The default table truncates the id with an ellipsis, so use `--json` if you need the full string:

```bash
memex lint findings --vault scratch --type quality --status pending \
  | grep propose_contradiction_winner
```

Or, for the full payload:

```bash
memex lint findings --vault scratch --type quality --status pending \
  --limit 5
```

The `evidence` payload on a `propose_contradiction_winner` finding carries:

- `winner_unit_id` and `loser_unit_id` — the two units in tension.
- `action` — one of `mark_loser_stale`, `supersede_loser_note`, `refine_not_contradict`, or `inconclusive`.
- `confidence` — the LLM's calibrated confidence in the proposal. Below the configured `propose_winner_min_confidence` (default `0.6`) the action is downgraded to `inconclusive` so the audit row exists but the apply path is blocked.
- `rationale` — one or two sentences explaining the LLM's reasoning.
- `linked_to_finding` — the upstream FSFM finding that triggered this proposal.

For the deploy-schedule case you should see `action: mark_loser_stale`, with the older unit as `loser_unit_id` and the newer one as `winner_unit_id`. The rationale will say something close to "the second note is more recent and explicitly supersedes the first". A trimmed example of the `evidence` payload:

```json
{
  "winner_unit_id": "7f44a2c1-...",
  "loser_unit_id":  "b3a1e8e0-...",
  "action": "mark_loser_stale",
  "confidence": 0.86,
  "rationale": "The second note is dated three weeks after the first and
    explicitly bans Friday-afternoon deploys, indicating the older policy
    no longer holds.",
  "linked_to_finding": "<uuid of the upstream FSFM finding>"
}
```

Confirm the loser id matches your `$OLD_UNIT_ID`:

```bash
echo "$OLD_UNIT_ID"
```

The two values should be identical. If they are not, you are looking at the wrong finding — re-list and find the one whose `loser_unit_id` matches.

## Step 6: Apply the winner

Apply the proposed action:

```bash
memex lint apply <finding_id>
```

You will see:

```
applied: <finding_id> (action=mark_loser_stale)
```

The action drives the mutation. For this case the loser unit's `status` flips from `'active'` to `'stale'`. The apply path captures the pre-mutation state under `evidence.resolution.prior_state` so the change is fully reversible. Confirm the status change directly:

```bash
memex memory view "$OLD_UNIT_ID"
```

You should see `Status: stale` in the header. The unit's content, source note, and entity links are untouched — only `status` changed.

The other actions the LLM might propose (and what they do):

| Action | Effect |
|---|---|
| `mark_loser_stale` | Flips the loser unit's `status` to `'stale'`. |
| `supersede_loser_note` | Sets the loser note's `superseded_by` to the winner note's id. Falls back to `mark_loser_stale` when both units share the same parent note. The fallback reason is recorded under `evidence.resolution.fallback_reason`. |
| `refine_not_contradict` | Rewrites the inbound `MemoryLink.link_type` from `'contradicts'` to `'refines'` (graph-pressure weight `0.0`). |
| `inconclusive` | No-op write; flips the finding to `resolved` without mutating any rows. |

## Step 7: Re-search and confirm the loser is now stale

Run the same search again:

```bash
memex memory search "when does the team deploy" --vault scratch
```

This time the older "5pm Friday" unit is gone from the results. Default retrieval excludes `status='stale'` rows entirely. The newer "Monday-Thursday window" unit stands alone.

To confirm the loser is still in the vault but suppressed, ask for it explicitly:

```bash
memex memory view "$OLD_UNIT_ID"
```

You will see `Status: stale` in the header. The unit row is preserved; it is just invisible to the default retrieval path. If you want to see stale rows in a search, pass `--include-stale`:

```bash
memex memory search "when does the team deploy" \
  --vault scratch --include-stale
```

Both units come back. The stale one is flagged for audit but no longer competes with the survivor on ranking.

You can also confirm the finding itself is now resolved:

```bash
memex lint findings --vault scratch --status resolved --limit 5
```

The `propose_contradiction_winner` row you applied appears here with `status=resolved`. Pending counts have dropped accordingly:

```bash
memex lint status --vault scratch
```

You should see one fewer pending finding than before.

## Step 8: Reverse the apply

The whole point of the audit trail is that the apply path is undoable. Suppose you decide the LLM was wrong — maybe the second note was a draft and the team never actually moved to the new window. Reverse it:

```bash
memex lint reverse <finding_id>
```

You will see:

```
reversed: <finding_id> (action=mark_loser_stale)
```

The reverse path reads `evidence.resolution.prior_state` and restores the affected row in a single transaction. It also writes a paired audit row (`rule_name=propose_contradiction_winner_reversal`) so the audit trail records both the apply and the reverse. The original finding stays in `resolved` status — the unique partial index on pending findings is preserved.

Run the search one more time:

```bash
memex memory search "when does the team deploy" --vault scratch
```

The older unit is back at `status='active'`, surfacing alongside the newer one. You are back where Step 3 left you, but with two audit rows recording everything that happened. <code-ref path="packages/cli/src/memex_cli/lint.py" lines="605-632" />

## What you built

You created a real contradiction in a vault, watched Memex detect it (a `contradicts` link), watched the LLM lint pass propose a fix (a `propose_contradiction_winner` finding), applied the fix (the loser unit went stale and dropped out of search), and reversed the fix (everything restored, with an audit row recording the round trip).

The mechanism behind every step is the same loop: extraction writes structured units, the contradiction engine writes typed links between disagreeing units, the rule-based lint pass flags units under enough pressure, the LLM lint pass proposes a winner with a calibrated action, and you (or an agent acting on your behalf) approve the action with `memex lint apply`. Nothing in this loop is destructive — every mutation captures its prior state, and the reverse path is one command away. <code-ref path="DESIGN_DOCUMENT.md" lines="401-403" />

## Next steps

- [How-to: Resolve contradictions](../how-to/linting.md) — the recipe-shaped version of this walkthrough, for when you already know the steps and just need the commands.
- [How-to: Linting](../how-to/linting.md) — the wider lint surface (other rule types, the interactive `memex lint review` triage flow, dismissing findings you do not want to act on).
- [Reference: CLI commands](../reference/cli.md) — every flag for `memex lint status`, `memex lint findings`, `memex lint apply`, `memex lint reverse`, `memex memory links`, and the rest.
- [Explanation: How Memex reconciles contradictions](../explanation/contradiction-detection.md) — the why behind the mechanism, including how the FSFM composite score puts units under graph pressure in the first place and where the LLM's confidence threshold comes from.

## See also

- [Tutorial: Walk through memory worth and deprioritization](memory-worth-and-deprioritization.md)
- [How-to: Resolve contradictions](../how-to/linting.md)
- [Reference: lint configuration](../reference/lint-config.md)
- [Explanation: contradiction detection](../explanation/contradiction-detection.md)
