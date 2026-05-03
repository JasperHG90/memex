"""F22 — Beta(1, 1) posterior over a unit's confidence (mean + variance).

Single-prior convention: matches F1a's ``Beta(success + 1, failure + 1)`` in
``services/outcomes.py:35-55`` so MW counters and confidence variance share
the same Bayes-Laplace uniform prior. No second prior to reason about.

Closed form
===========

With prior ``Beta(1, 1)``, posterior parameters are::

    alpha = 1 + confidence × confidence_evidence_count
    beta  = 1 + (1 − confidence) × confidence_evidence_count

The posterior mean is the input ``confidence`` itself (counts shape variance,
not the mean — by construction the posterior preserves the supplied
confidence as its central tendency). The posterior variance is the standard
Beta variance ``(α × β) / ((α + β)² × (α + β + 1))``.

Cold-start (``confidence_evidence_count = 0``) collapses the posterior to the
Beta(1, 1) prior — variance = ``MAX_VARIANCE = 1/12``, certainty = 0,
F47 boost neutral.
"""

from __future__ import annotations

# Variance of Uniform(0, 1) = Beta(1, 1) — the cold-start ceiling.
# Pinned in tests so a future prior change is a deliberate edit.
MAX_VARIANCE: float = 1.0 / 12.0


def mean_and_variance(confidence: float, evidence_count: int) -> tuple[float, float]:
    """Closed-form Beta(1, 1) posterior mean + variance.

    Returns:
        ``(mean, variance)`` where mean is the input ``confidence`` and
        variance is in ``[0, MAX_VARIANCE]``. Variance is ``MAX_VARIANCE``
        at ``evidence_count = 0`` (cold-start: posterior collapses to prior)
        and shrinks toward 0 as evidence accumulates.
    """
    alpha = 1.0 + confidence * evidence_count
    beta = 1.0 + (1.0 - confidence) * evidence_count
    n = alpha + beta
    variance = (alpha * beta) / (n * n * (n + 1.0))
    return confidence, variance


def certainty(confidence: float, evidence_count: int) -> float:
    """Certainty factor in ``[0, 1]`` — used by the F22 F47 boost extension.

    ``certainty = 1 − variance / MAX_VARIANCE`` so cold-start (count=0) → 0
    (boost collapses to neutral) and well-evidenced → ~1 (full F47 lift).
    """
    _, variance = mean_and_variance(confidence, evidence_count)
    return 1.0 - variance / MAX_VARIANCE


def sample_concentration(alpha: float, beta: float) -> float:
    """Concentration parameter ``α + β`` of a Beta posterior.

    Used by reflection-cron prioritisation: low concentration → wide posterior
    → high information yield from re-evaluation.
    """
    return alpha + beta
