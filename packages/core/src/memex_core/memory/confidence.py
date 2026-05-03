"""F22 — Beta(1, 1) posterior over a unit's confidence (mean + variance).

Single-prior convention: matches F1a's ``Beta(success + 1, failure + 1)`` in
``services/outcomes.py:35-55`` so MW counters and confidence variance share
the same Bayes-Laplace uniform prior. No second prior to reason about.

Closed form
===========

With prior ``Beta(1, 1)``, posterior parameters are::

    alpha = 1 + confidence × confidence_evidence_count
    beta  = 1 + (1 − confidence) × confidence_evidence_count

The actual Beta-posterior mean is ``alpha / (alpha + beta) = (1 + c·n) /
(2 + n)``, which only equals the input ``confidence`` ``c`` when ``n = 0``
or in the ``n → ∞`` limit (e.g. for ``c = 0.3, n = 20`` the true posterior
mean is ``7/22 ≈ 0.318 ≠ 0.3``).

This module deliberately uses the **input ``confidence``** as the
*point estimate* downstream scoring composes against — F22 treats the
stored ``confidence`` field as the canonical mean and uses
``confidence_evidence_count`` only to *shape variance* (and therefore the
``certainty`` factor that gates the F47 boost). The full Beta-posterior
mean is intentionally NOT used here so the boost site at
``retrieval/engine.py`` doesn't drift away from the row's
``confidence`` value. ``mean_and_variance`` therefore returns
``(confidence, beta_posterior_variance)``.

The posterior variance is the standard Beta variance
``(α × β) / ((α + β)² × (α + β + 1))``.

Cold-start (``confidence_evidence_count = 0``) collapses the posterior to the
Beta(1, 1) prior — variance = ``MAX_VARIANCE = 1/12``, certainty = 0,
F47 boost neutral.
"""

from __future__ import annotations

from typing import Any

# Variance of Uniform(0, 1) = Beta(1, 1) — the cold-start ceiling.
# Pinned in tests so a future prior change is a deliberate edit.
MAX_VARIANCE: float = 1.0 / 12.0


def extract_confidence_and_count(unit: Any) -> tuple[float, int]:
    """Defensively extract ``(confidence, confidence_evidence_count)`` from a unit.

    Single source of truth (Hermes round-4 MED) — used by ``engine.py``,
    ``reflection.py:_variance_key``, and ``exploration.py:_unit_variance``
    so the falsy-zero handling, ``None`` fallback, and integer cast cannot
    drift across call sites.

    Behaviour:
      - ``confidence`` falls back to ``1.0`` when the attribute is missing
        OR explicitly ``None`` (stripped/stale model rows). ``0.0`` is
        preserved verbatim — it is a legitimate value (a unit thoroughly
        contradicted to zero) and must NOT be coerced via ``or 1.0``.
      - ``confidence_evidence_count`` falls back to ``0`` on missing/None.
    """
    raw_confidence = getattr(unit, 'confidence', 1.0)
    confidence = 1.0 if raw_confidence is None else float(raw_confidence)
    raw_count = getattr(unit, 'confidence_evidence_count', 0)
    evidence_count = 0 if raw_count is None else int(raw_count)
    return confidence, evidence_count


def mean_and_variance(confidence: float, evidence_count: int) -> tuple[float, float]:
    """Closed-form Beta(1, 1) posterior variance + the unit's input mean.

    Returns:
        ``(mean, variance)`` where ``mean`` is the **input** ``confidence``
        (used as the F22 point estimate for downstream scoring — see this
        module's docstring) and ``variance`` is the Beta-posterior variance
        in ``[0, MAX_VARIANCE]``. Variance is ``MAX_VARIANCE`` at
        ``evidence_count = 0`` (cold-start: posterior collapses to prior)
        and shrinks toward 0 as evidence accumulates.

        The returned ``mean`` is NOT the Beta-posterior mean
        ``(1 + c·n) / (2 + n)`` — F22 deliberately uses the row's stored
        confidence as the point estimate so the F47 boost stays anchored
        on the same value the lint gate and contradiction engine see.

    Hermes round-6 MED: defends against bad upstream input. ``confidence``
    must lie in ``[0, 1]`` for the Beta(α, β) shape parameters to stay
    non-negative; an out-of-range value (e.g. an upstream bug writing
    ``1.0001``) would produce a negative ``beta`` and a nonsensical
    variance. The DB CHECK constraint and DTO field validators protect
    the happy path, but this is the canonical helper — a direct call
    with bad input now surfaces at the computation site rather than
    silently returning garbage downstream. ``evidence_count`` must be
    non-negative for the same reason.
    """
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f'confidence must be in [0, 1]; got {confidence!r}. '
            'F22 invariant: the Beta(1, 1) posterior shape parameters '
            'go negative outside this range.'
        )
    if evidence_count < 0:
        raise ValueError(f'evidence_count must be non-negative; got {evidence_count!r}.')
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
