# Observability

Memex exposes operational telemetry through a Prometheus-compatible `/api/v1/metrics` endpoint (provided by `prometheus-fastapi-instrumentator`). All metrics declared in `packages/core/src/memex_core/metrics.py` are registered with the default registry and surface here automatically.

This page focuses on the reranking-composition metrics needed to tune the `composite_boost_log_clip` configuration knob. Other categories (ingestion, retrieval duration, LLM calls, circuit breaker, reflection queue, lint findings, contradiction resolution, entity-collapse maintenance, FSFM scoring, etc.) are declared in the same module and exposed through the same endpoint without further configuration.

## Endpoint

`GET /api/v1/metrics` — Prometheus text exposition format. The endpoint is exempt from authentication by default; see [Configuration → `exempt_paths`](configuration.md) to adjust.

## Reranking composition metrics

The cross-encoder reranker composes five multiplicative boost factors — recency, temporal, Memory Worth, confidence, and decay — and applies the product to the cross-encoder score. The composition is implemented in log space with a symmetric clip; see [Retrieval scoring](../explanation/retrieval-scoring.md) for the rationale.

### `memex_composite_boost_clipped`

**Type:** Histogram (no labels)

**Buckets:** `0.1, 0.3, 0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0`

**Emits on:** every reranking candidate, once per `(query, candidate)` pair, after the five-factor composition has been computed.

**Observed value:** `exp(clip(sum(log(b_i)), -L, +L))`, where `b_i` are the five boost factors and `L = composite_boost_log_clip`.

**Dual interpretation by clip mode:**

| `composite_boost_log_clip` | What the histogram measures |
|---|---|
| `math.inf` (ship default) | The pre-clip aggregate product itself — clip is a no-op, so the observed value equals the raw `b_1 · b_2 · b_3 · b_4 · b_5` for inputs above the per-factor floor `LOG_FLOOR_COMPOSITE_BOOST = 1e-9` (`engine.py:82`). Any factor at or below the floor is clipped to `1e-9` before the log, so a single zero-or-negative factor yields `ce_score · ~1e-9` rather than `ce_score · 0`. Above the floor the round-trip through five logs plus one exp introduces a few ULPs of float-precision drift. Read this distribution to choose a finite `L`. |
| Finite `L` (e.g. `0.7`, `1.0`, `1.5`) | The **post-clip** aggregate multiplier actually applied to `ce_score`. Every observation is mathematically inside `[exp(-L), exp(+L)]` by construction. Mass accumulating at the bucket boundaries containing `exp(-L)` and `exp(+L)` is the unambiguous "clip is firing" signal — the unclipped product would have been outside the band. A smooth distribution strictly interior to the band means the clip is dormant for typical traffic. |

At the ship default, this metric subsumes a dedicated pre-clip histogram — the distribution needed to derive an empirical `L` is observable directly. Once `L` is moved off `math.inf`, the same metric continues to be useful, but it answers a different question (post-clip vs pre-clip). A separate dedicated pre-clip histogram is **not** currently shipped; the decision and rationale are recorded under [Decision: pre-clip histogram for the finite-L regime](#decision-pre-clip-histogram-for-the-finite-l-regime).

**How to tune `L`:**

1. Let the histogram accumulate representative traffic at `L = math.inf` for ≥ 1 week.
2. Compute the empirical p95 (or whichever upper-tail percentile the deployment prefers) of `|log(observed_value)|`. The histogram buckets cover the realistic range; if production traffic clusters tightly around `1.0`, even `L = 0.7` is a wide band.
3. Set `composite_boost_log_clip = L` via the standard configuration path. The clip becomes active at the next reranker call; no restart is required if the deployment uses a reloadable config.
4. Re-run the retrieval-ranking regression evaluation (`uv run pytest -m benchmark` for the local retrieval benchmarks, plus the LoCoMo eval if the deployment exercises that scenario) to confirm ranking drift stays within tolerance for the deployment's quality budget.

### `memex_composite_boost_non_finite_guard_triggered_total`

**Type:** Counter (no labels)

**Emits on:** any reranking call where the guard at `packages/core/src/memex_core/memory/retrieval/engine.py:128-135` short-circuits. The guard fires when:

- `ce_score` is non-finite (`NaN`, `+inf`, or `-inf`), or
- any of the five boost factors is non-finite (`NaN`, `+inf`, or `-inf`), or
- `composite_boost_log_clip` is `NaN`, or
- `composite_boost_log_clip` is negative.

`composite_boost_log_clip = math.inf` is the supported ship default and does **not** trigger the guard — `+inf` is treated as "no clip" rather than "non-finite". When the guard fires it returns `ce_score` unmodified and skips the `Histogram.observe` call, so corrupted observations do not pollute `memex_composite_boost_clipped`.

**Operational meaning:** a non-zero rate indicates upstream calibration or configuration drifted into territory the composer rejects — typically a `Decimal` → `float` conversion that emitted `NaN`, a Memory Worth feature that produced `inf`, a misset `composite_boost_log_clip` (e.g., parsed config value that resolved to `nan`), or a negative `composite_boost_log_clip` (which would invert the clip semantics). Investigate before relying on `memex_composite_boost_clipped` for tuning decisions.

### Per-factor histograms

Three of the five boost factors emit their own pre-composition histograms (all unlabeled):

- `memex_mw_boost` — Memory Worth factor.
- `memex_confidence_boost` — contradiction-derived confidence factor.
- `memex_decay_boost` — FSFM-lite decay factor.

Divergent shape between any of these and `memex_composite_boost_clipped` localises which heuristic is doing the most work in production traffic.

The remaining two factors — **recency** and **temporal** — do not currently have dedicated per-factor histograms. Their contribution is only observable via `memex_composite_boost_clipped` (and via eval runs that compare composite-only ranking against ranking with the corresponding alpha set to `0`).

## Decision: pre-clip histogram for the finite-L regime

A separate, dedicated pre-clip histogram (proposed name: `memex_aggregate_boost_preclip`) was considered for the regime where `composite_boost_log_clip` is finite and `memex_composite_boost_clipped` therefore measures the post-clip multiplier rather than the raw product. It is **not shipped** at this time.

**Rationale for deferral:**

- At the ship default `L = math.inf` the existing histogram already observes the pre-clip product directly. Operators tuning `L` for the first time do not need a second metric.
- Adding a second unlabeled histogram with overlapping bucket ranges doubles the observation cost on every reranking candidate (currently dominated by cross-encoder inference, but worth measuring before committing) and adds an export-side cardinality of zero — so the operational concern is purely declaration overhead.
- The need is conditional on real operator demand once `L` becomes finite. Anyone who wants to compare pre- and post-clip distributions can extract the same information from raw scores during eval runs.

**Reopen the question** if any of the following hold after `L` is moved off `math.inf` in production:

- Operators report they cannot answer "what would the reranker have applied without the clip?" from existing metrics.
- An eval run requires a side-by-side pre/post comparison and re-deriving from raw scores is impractical.
- The clip rate (fraction of `memex_composite_boost_clipped` observations at the bucket boundaries `exp(-L)` and `exp(+L)`) is high enough that the post-clip view is materially different from the pre-clip view.

If reopened, the metric declaration should live next to `COMPOSITE_BOOST_CLIPPED` in `packages/core/src/memex_core/metrics.py` and be observed at the same call site one statement earlier in the composition.
