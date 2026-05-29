# How the inbox router works

The inbox router solves a small but nagging problem: you have something worth
keeping but you don't want to decide *where* it goes right now. You drop it in
an `inbox` vault and move on. The router's job is to later look at each inbox
note and answer "which vault does this belong to?" — confidently enough to move
it for you, or, when it isn't sure, to hand you a short list to pick from.

## Why a classifier, and why this one

Routing a note to a vault is a classification problem: given a note, which of N
vaults is the best home? A twelve-iteration proof-of-concept compared several
approaches (rank fusion à la TEMPR, multinomial logistic regression, several
Naive Bayes variants). The winner was a **pairwise Gaussian Naive Bayes** model
over five cheap features, for three reasons:

1. **It runs entirely in Postgres.** No model artifact to load, no Python
   inference service. Scoring is one SQL query; "training" is arithmetic on
   running sums.
2. **It learns online.** Every confirmed route is one conjugate update — a
   single `UPDATE` of sufficient statistics. The model improves as you use it
   without a retrain job.
3. **It degrades gracefully.** Each note is scored against each vault
   independently as a "does this pair match?" question, so adding or removing a
   vault needs no retraining.

## The five features

For a (note, vault) pair the router computes:

- `sem_summary_sim` — cosine between the note and the vault's reflected summary.
- `sem_centroid_sim` — cosine between the note and the mean of the vault's notes.
- `mm_centroid_sim` — cosine between the note and the vault's mental-model centroid.
- `entity_jaccard` — overlap between the note's entities and the vault's top-100
  entities (by mention count — the vault's *core* topics, not every entity it has
  ever brushed against).
- `keyword_ts_rank` — Postgres full-text rank of the note's keywords against the
  vault's text.

Per-vault anchors (centroids, the entity set, the full-text document) are cached
in `inbox_router_vault_anchors` and refreshed each tick, so scoring never has to
re-aggregate a whole vault. The note's side is cached in
`inbox_router_note_cache`.

## How "training" works without training

Gaussian Naive Bayes needs, per feature and per class (match / no-match), a mean
and a variance. Those are recoverable from three running sums: the count, the
sum of values, and the sum of squares. The router stores exactly those in
`inbox_router_nb_stats`, and a view (`inbox_router_nb_params`) derives `(μ, σ²)`
from them on the fly. Recording an observation is therefore just:

```
n      ← γ·n      + 1
Σx     ← γ·Σx     + x
Σx²    ← γ·Σx²    + x²
```

The `γ` (default 0.99) is an exponential-decay factor: it lets the model slowly
forget stale statistics as a vault's character drifts, rather than weighting a
note from a year ago the same as one from yesterday.

### The cold-start prior

A brand-new install has no confirmed routes. A flat prior here is actively
harmful: a zero-variance Gaussian makes the likelihood explode and the rankings
collapse to noise. So the stats table ships **seeded** from the proof-of-concept's
measured per-feature means and variances, at a deliberately weak weight
(equivalent to five observations). The router therefore produces sensible
rankings from the very first tick, and your real decisions quickly outweigh the
seed.

## From score to action

The per-vault match probabilities are turned into a per-note distribution
(softmax) so the candidates compete. Then a simple policy decides:

- **Auto-route** when the model is warmed up, the top vault's probability clears
  a floor, *and* it beats the runner-up by a margin. In the POC, correct routes
  beat the runner-up by a median margin of 0.81; wrong ones by 0.10 — so the
  margin gate is what makes hands-off routing safe.
- **Propose** the top three candidates to the cockpit when it's plausible but not
  decisive.
- **No-fit** when nothing clears the floor; the note stays in the inbox and the
  router backs off (exponentially) before looking again.

Two guards keep auto-routing conservative: a **warm-up gate** (auto-route stays
off until enough confirmed routes accumulate) and a **per-tick cap** (a
mis-scoring run can move at most a handful of notes before the rest fall through
to proposals).

## Why the probabilities are a *score*, not a calibrated confidence

Naive Bayes assumes features are independent; ours aren't (`sem_summary_sim` and
`sem_centroid_sim` both lean on the note embedding). So a `p_match` of 0.92 does
not literally mean "92% likely correct" — it's a well-behaved ranking score. That
is why the auto-route gate leans on the *margin* between candidates and an
empirically chosen floor rather than treating the probability as ground truth.

## Auditing and reversal

Every auto-route is recorded as a resolved `inbox_vault_route` proposal stamped
`system:inbox-router`, so there is always a trail. Routing is performed through
the same reversible `route_note_to_vault` action the cockpit uses, so a route —
auto or manual — can be undone, moving the note back to where it came from.

## Known limitations

- The router scores against existing vaults; it never invents a new one.
  Repeated no-fit clusters are a signal you may want a new vault, but creating
  one is left to you (a future enhancement could suggest it).
- Probabilities are ranking scores, not calibrated confidences (see above).
- Auto-routing is intentionally cautious early on; expect to confirm proposals
  by hand until the model warms up.
