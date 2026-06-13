---
name: correct
description: "Tell Memex a surfaced memory was wrong or stale. Records a negative outcome on the offending unit(s) and lowers their retrieval rank — the negative-signal loop."
argument-hint: "[what was wrong/stale, e.g. 'the rotation cadence it suggested is outdated']"
---

# /correct — Mark a memory wrong or stale

When Memex surfaced a fact that was misleading, outdated, or noise, this skill closes the negative-signal loop: it stamps a `not_helpful` outcome on the unit and deprioritizes it so it stops contaminating future results. Both signals are orthogonal — the outcome is an append-only gradient on the unit's Memory Worth; deprioritize is a reversible surface-state flag.

## 1. Disambiguate first

If there's no concrete referent in scope — a vague "that was wrong" with nothing pinned down — **ASK which fact / which suggestion**. Never guess a `unit_id`, and never fabricate a target from search results. Acting on a guessed unit corrupts the wrong memory.

## 2. Find the candidate unit(s)

Search for the unit(s) the correction is about: `memex_memory_search(query="...", top_k=30)` (use `top_k >= 30` — corrections need a wide candidate set). **READ the unit bodies** and pick the specific subset the correction applies to. Do not bulk-act on the whole result set.

## 3. Resolve observations BEFORE acting

Some hits are **observations** — read-only projections of memory units, marked `unit_metadata.virtual: true`. You cannot deprioritize a virtual unit: the call is rejected, and the redirect to the underlying units does **not** survive back to you as a usable error. So check each hit **before** acting:

- If a hit has `unit_metadata.virtual: true`, do not target its UUID. Resolve it to the underlying memory unit(s) from the search result's evidence / `source_memory_units` and target those instead.
- Only ever record outcomes and deprioritize **real** memory units.

## 4. Paired write on the judged subset

For each real unit you're correcting, do both:

```text
memex_record_outcome(units=[{unit_id, verb: "not_helpful", reason}])
memex_memory_deprioritize(unit_id, reason, vault_id=<the unit's vault>)
```

- `reason` is required and free-text — say *why* it's wrong/stale.
- **Pass the unit's own `vault_id`** (from its search hit). `memex_memory_deprioritize` defaults to the active write vault and rejects cross-vault calls — a candidate that lives in another vault will fail without it.
- `memex_record_outcome` takes `units=[{unit_id, verb, reason}]`; a bare `success=true/false` returns an error.

## 5. Confirm and offer undo

State which units you corrected and the reason. Deprioritize is reversible: if the user says you over-corrected, `memex_memory_restore(unit_id, vault_id)` flips it back into default-scope retrieval. (Recording an outcome is append-only and not reversed — restore only undoes the surface state.)

This is the mirror image of confirming a memory *held* — for "that worked / it's holding", record a `helpful` outcome instead (no deprioritize). Keep this consistent with the system-prompt resolution flow that `/remember` follows.
