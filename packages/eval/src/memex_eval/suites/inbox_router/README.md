# inbox_router suite

Gates the **inbox router's routing accuracy**: given notes dropped in the
`inbox` vault, does the router route each to the topically-correct vault?

## What it does

The `seed_inbox_router_corpus` setup action (in `_setup_actions.py`):

1. Creates two topically-distinct target vaults (`eval-router-cooking`,
   `eval-router-gardening`) and ingests a few on-topic notes into each.
2. Ingests a set of **labelled** notes into the `inbox` vault — each note
   clearly belongs to one target vault.
3. Waits for extraction in every vault (chunks, embeddings, entities).
4. Triggers a **dry-run** triage (`POST /api/v1/inbox/triage?dry_run=true`),
   which scores + decides without mutating, and returns per-note predictions.
5. Publishes `{predictions, expected}` (keyed by note UUID) into the scenario
   context.

The `inbox_route_accuracy` outcome compares the router's top-1 candidate per
note to the label and passes when accuracy ≥ `min_accuracy` (default 0.75).

## Scope

- **In scope:** end-to-end routing accuracy over the live server — anchor
  refresh, per-note feature cache, pairwise GaussianNB scoring, and the decision
  policy. The NB prior is seeded by the service, so this measures the seeded
  model (no warm-up), which is the day-1 behaviour an operator sees.
- **Out of scope:** auto-apply gating, the cockpit proposal lifecycle, online
  learning over many decisions, and no-fit backoff — those are covered by the
  unit + integration tests (`packages/core/tests/.../inbox_router`).

## Running

```bash
memex-eval suite run inbox_router
```

Requires a running server + Postgres (the runner provisions both). The corpus is
deliberately small and topically unambiguous, so a correctly-wired router should
clear the accuracy bar comfortably; a regression in scoring, anchors, or the
decision policy drops it.
