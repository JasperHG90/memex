# Retrieval scoring — composition discipline

Memex's reranker stacks five metadata boost factors on top of a sigmoid-normalised cross-encoder score (`ce_score ∈ [0, 1]`):

- `recency_boost` — event-date recency, linearly ramped over the past 365 days
- `temporal_boost` — query-side temporal-proximity match (e.g. "last March")
- `mw_boost` — Memory Worth (Beta-Bernoulli posterior over success / failure outcomes)
- `confidence_boost` — contradiction-derived confidence, optionally certainty-modulated
- `decay_boost` — FSFM-lite Ebbinghaus decay × importance

Each factor is bounded by its own `…_alpha` field on `RetrievalConfig`. The composition step combines them into a single multiplier on `ce_score`.

## Why log-additive with a clip

A pure multiplicative chain compounds noise as well as signal. Five bounded factors each in roughly `[0.5, 1.5]` give an aggregate product in roughly `[0.03, 7.6]`: a single noisy heuristic can either veto a high-`ce_score` candidate (drag it down by 30×) or amplify a low-`ce_score` candidate (lift it up by 7×). Neither is desirable when the cross-encoder is the strongest signal we have.

The composition is expressed in log-additive form:

```
log_aggregate = log(recency) + log(temporal) + log(mw) + log(confidence) + log(decay)
clipped       = max(-L, min(L, log_aggregate))
final_score   = ce_score * exp(clipped)
```

where `L = composite_boost_log_clip` is a per-config knob.

- At `L = math.inf` (ship default), the clip is a no-op and the result is mathematically identical to the prior `ce_score × b₁ × b₂ × b₃ × b₄ × b₅` product for strictly positive boost inputs (modulo 1e-9 floating-point). For a boost that hits exactly `0.0` — reachable only when an alpha is non-zero and a unit was contradicted to zero, dormant under ship defaults — the new form preserves rank ordering by `ce_score` instead of tying at `0.0`; see [Floor for zero / negative boost values](#floor-for-zero--negative-boost-values).
- At finite `L`, the aggregate metadata multiplier is bounded to `[exp(-L), exp(+L)]`. The per-factor alpha knobs still bound each individual factor; the clip bounds the *product*. This prevents both single-factor noise and compound noise from dominating `ce_score`.

| `L` value | Aggregate multiplier bounds |
|:----------|:----------------------------|
| `0.0`     | `[1.0, 1.0]` — metadata fully disabled |
| `0.5`     | `[0.61, 1.65]` — gentle clamp (≈ 2.7× swing) |
| `0.7`     | `[0.50, 2.01]` — moderate clamp (≈ 4× swing) |
| `1.0`     | `[0.37, 2.72]` — wider clamp (≈ 7× swing) |
| `1.5`     | `[0.22, 4.48]` — loose clamp (≈ 20× swing) |
| `math.inf` | unbounded — identical to the prior multiplicative product |

## Picking `L`

`L = math.inf` (ship default) is the safe starting point: it preserves the prior behavior exactly. To tighten the clip, observe the aggregate distribution first:

1. Let the `memex_composite_boost_clipped` histogram (post-clip) accumulate representative traffic for ≥ 1 week. At the ship default `L = math.inf` the clip is a no-op, so this metric observes the **pre-clip aggregate product directly** — exactly the distribution needed to tune `L`. See [Observability → Reranking composition metrics](../reference/observability.md#reranking-composition-metrics) for the dual interpretation and the deferred-decision rationale on a dedicated pre-clip histogram.
2. Compute the empirical distribution of `|log(aggregate)|`. The `p95` of that distribution is a sensible upper bound for `L`: it accepts 95% of observed compositions unchanged and clips only the extreme tail.
3. Land an empirical `L` via a follow-up config commit (no code change needed).

Once `L` is finite, the same `memex_composite_boost_clipped` histogram shows what the reranker actually applied **after** clipping, so the operator can confirm the clip behaves as intended without re-deriving from raw scores.

## Floor for zero / negative boost values

The helper floors each individual boost at `LOG_FLOOR_COMPOSITE_BOOST = 1e-9` before taking the log. This handles two corner cases without raising:

- A boost legitimately at exactly `0.0` (e.g. `confidence_boost` when `confidence_alpha > 0` and a unit was contradicted to zero). The product form returns exactly `0.0` for any zero factor; the log-additive form returns roughly `ce_score × 1e-9` when a single factor hits the floor (`ce_score × 1e-9^k` when `k` factors hit; the degenerate all-five case is `ce_score × 1e-45`). Both effectively collapse the unit to the bottom of the ranking, but the log-additive form preserves rank ordering by `ce_score` instead of tying every zero-boost unit at exactly zero. At ship-default alphas (`confidence_alpha = decay_alpha = 0.0`) the zero-boost path is dormant.
- A boost that goes negative through a future bug or misconfiguration. The floor produces a finite result instead of `math.log(<negative>)` raising `ValueError`.
