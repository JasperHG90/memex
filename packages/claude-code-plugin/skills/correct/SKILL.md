---
name: correct
description: "Tell Memex a surfaced memory was wrong, outdated, or noise. Use whenever you say 'stop suggesting that', 'that's stale / outdated / wrong', 'we removed that', or want a misleading fact to stop surfacing — records a not_helpful outcome on the offending unit(s) and deprioritizes them (the negative-signal loop). NOT for confirming a fix worked (that's a helpful outcome)."
argument-hint: "[which fact + why it's wrong, e.g. 'the rotation cadence it suggested is outdated']"
---

# /correct — Mark a memory wrong or stale

When Memex surfaced a fact that was misleading, outdated, or noise, this skill closes the negative-signal loop: it stamps a `not_helpful` outcome on the unit and deprioritizes it so it stops contaminating future results. Both signals are orthogonal — the outcome is an append-only gradient on the unit's Memory Worth; deprioritize is a reversible surface-state flag.

## 1. Disambiguate first

If there's no concrete referent in scope — a vague "that was wrong" with nothing pinned down — **ASK which fact / which suggestion**. Never guess a `unit_id`, and never fabricate a target from search results. Acting on a guessed unit corrupts the wrong memory.

## 2. Find the candidate unit(s)

Search for the unit(s) the correction is about: `memex_memory_search(query="...", top_k=30)` (use `top_k >= 30` — corrections need a wide candidate set). **READ the unit bodies** and pick the specific subset the correction applies to. Do not bulk-act on the whole result set.

## 3. Resolve observations BEFORE acting

Some hits are **observations** — read-only projections of memory units. Each hit carries a top-level `virtual` flag; when `virtual: true`, its `id` is a synthesized placeholder, not a deprioritizable row. Check it before acting:

- If a hit has `virtual: true`, do NOT target its `id`. Resolve to the underlying memory unit(s) via the hit's **`evidence_ids`** and target those instead.
- If you do try to deprioritize a virtual unit, the tool now returns an error that lists the source memory unit(s) to retry against — so the reactive path works as a safety net. But checking `virtual` up front (and using `evidence_ids`) is cleaner and avoids the round-trip.
- Only ever record outcomes and deprioritize **real** memory units.

## 4. Paired write on the judged subset

For each real unit you're correcting, do both:

```text
memex_record_outcome(units=[{unit_id, verb: "not_helpful", reason}])
memex_memory_deprioritize(unit_id, reason)
```

- `reason` is required and free-text — say *why* it's wrong/stale.
- `memex_record_outcome` takes `units=[{unit_id, verb, reason}]`; a bare `success=true/false` returns an error.
- `memex_memory_deprioritize` targets the active write vault by default — the same vault `memex_memory_search` drew the hit from — so you don't pass a vault. (Only set `vault_id` if you deliberately searched another vault; a wrong vault is rejected.)

## 5. Confirm and offer undo

State which units you corrected and the reason. Deprioritize is reversible: if the user says you over-corrected, `memex_memory_restore(unit_id)` flips it back into default-scope retrieval. (Recording an outcome is append-only and not reversed — restore only undoes the surface state.)

This is the mirror image of confirming a memory *held* — for "that worked / it's holding", record a `helpful` outcome instead (no deprioritize). Keep this consistent with the system-prompt resolution flow that `/remember` follows.
