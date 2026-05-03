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

import math
from typing import Protocol, runtime_checkable

# Variance of Uniform(0, 1) = Beta(1, 1) — the cold-start ceiling.
# Pinned in tests so a future prior change is a deliberate edit.
MAX_VARIANCE: float = 1.0 / 12.0


@runtime_checkable
class HasConfidence(Protocol):
    """Structural type for objects carrying F22 confidence + evidence-count fields.

    Hermes round-13 LOW: replaces the prior ``Any`` annotation on
    ``extract_confidence_and_count`` so mypy can verify each call site
    (engine, reflection, exploration) actually passes a unit-shaped object
    rather than tunneling arbitrary values through the helper. Both
    attributes are typed ``| None`` because production rows can carry
    ``NULL`` (cold-start, stripped/stale model rows); the helper itself
    handles the ``None`` fallback.

    Type-only contract (Hermes round-14 LOW + round-18 LOW): the
    Protocol is decorated ``@runtime_checkable`` so ``isinstance``
    works, but the helper that consumes it uses ``getattr`` with
    defaults — a ``dict``, a bare ``object()``, or any value will pass
    the static type check AND not raise at runtime; the helper just
    returns ``(1.0, 0)``. This is the documented contract: the Protocol
    is a *static* hint to mypy, NOT a runtime invariant. Do not rewrite
    the helper body to read ``unit.confidence`` directly without first
    auditing every call site for objects that satisfy the Protocol
    via ``__getattr__`` magic rather than concrete attributes.
    """

    confidence: float | None
    confidence_evidence_count: int | None


def extract_confidence_and_count(unit: HasConfidence) -> tuple[float, int]:
    """Defensively extract ``(confidence, confidence_evidence_count)`` from a unit.

    Single source of truth (Hermes round-4 MED) — used by ``engine.py``,
    ``reflection.py:_variance_key``, and ``exploration.py:_unit_variance``
    so the falsy-zero handling, ``None`` fallback, and integer cast cannot
    drift across call sites.

    Protocol vs. runtime gap (Hermes round-14 LOW): the ``HasConfidence``
    annotation gives mypy a structural contract for the three first-party
    callers, but the body uses ``getattr`` with explicit defaults so a
    ``MemoryUnit`` instance with neither attribute (e.g. a stripped DTO
    or a partial mock) does NOT raise — it returns the documented
    fallbacks. The intent is defensive parity with the column NULL-able
    fallback the storage layer already provides; do not interpret the
    Protocol as a runtime invariant.

    Behaviour:
      - ``confidence`` falls back to ``1.0`` when the attribute is missing
        OR explicitly ``None`` (stripped/stale model rows). ``0.0`` is
        preserved verbatim — it is a legitimate value (a unit thoroughly
        contradicted to zero) and must NOT be coerced via ``or 1.0``.
        Out-of-range values (e.g. an in-flight ``MemoryUnit`` carrying
        ``1.0001`` from a concurrent write before the SQL-level
        ``GREATEST/LEAST`` clamp settles) are clamped to ``[0, 1]``
        rather than letting ``mean_and_variance`` raise — Hermes round-8
        HIGH: an unhandled ``ValueError`` in the retrieval-rerank path
        would 500 the request. This mirrors the ``schemas.py`` DTO
        validator's defence-in-depth clamp.
      - ``confidence_evidence_count`` falls back to ``0`` on missing/None
        and is floored at ``0`` for the same reason. Hermes round-20 MED:
        a non-integer ``raw_count`` (e.g. an in-flight ``2.9`` mid-pipeline)
        is rounded via half-up semantics (``int(math.floor(x + 0.5))``)
        rather than truncated via ``int()`` — ``int(2.9) = 2`` silently
        drops nearly a full evidence event, which biases variance
        downward. Hermes round-21 MED: half-up is used instead of the
        builtin ``round()`` to avoid Python's banker's rounding
        (``round(2.5) = 2``, not 3); for an evidence counter, the
        intuitive "round halves up" is what callers expect.

    Defensive-only path (Hermes round-22 MED)
    -----------------------------------------
    The half-up rounding only fires for a non-integer ``raw_count``,
    which violates the upstream invariant. The DB column
    ``memory_units.confidence_evidence_count`` is ``INTEGER NOT NULL``
    (CHECK ``>= 0``), and the contradiction engine writes ``+1``
    integer bumps via SQL arithmetic — so production rows always
    carry an integer value. The fractional-input branch exists to
    keep ``mean_and_variance`` from raising on a bad in-memory mock
    or stale test fixture, NOT as a normal-operation code path.
    """
    raw_confidence = getattr(unit, 'confidence', 1.0)
    confidence = 1.0 if raw_confidence is None else float(raw_confidence)
    # NaN / inf guard (Hermes round-24 HIGH): a non-finite confidence
    # would survive the ``min``/``max`` clamp (Python's NaN propagates
    # through both) and produce a non-finite Beta shape parameter,
    # silently tainting downstream variance with ``NaN``. The DB CHECK
    # constraint prevents this for production rows; the guard keeps
    # the defensive path safe for in-memory mocks / stale model
    # objects. ``NaN`` falls back to ``1.0`` for parity with the
    # missing-attribute branch above.
    if not math.isfinite(confidence):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))
    raw_count = getattr(unit, 'confidence_evidence_count', 0)
    if raw_count is None:
        evidence_count = 0
    elif isinstance(raw_count, int) and not isinstance(raw_count, bool):
        # Production fast path (Hermes round-23 MED): the DB column is
        # ``INTEGER NOT NULL`` so ``raw_count`` is an ``int`` for every
        # production row. Skip the float-round path entirely to avoid
        # the ``2**53 + 1`` precision loss in ``float()`` and the
        # ``math`` import overhead. ``bool`` is excluded because
        # ``isinstance(True, int)`` is True in Python — a confused
        # caller passing ``True`` would otherwise short-circuit to 1.
        evidence_count = raw_count
    elif isinstance(raw_count, float) and not math.isfinite(raw_count):
        # NaN / inf guard (Hermes round-24 HIGH): ``float('nan')`` and
        # ``float('inf')`` would survive the half-up round below
        # (``math.floor(NaN + 0.5)`` is ``NaN``, then ``int(NaN)``
        # raises ``ValueError``). Fall back to 0 so the rerank path
        # cannot 500 on a stale in-memory unit.
        evidence_count = 0
    else:
        # Half-up rounding (Hermes round-21 MED): ``math.floor(x + 0.5)``
        # gives ``2.5 → 3`` and ``2.9 → 3`` consistently, unlike the
        # builtin ``round()`` which uses banker's rounding.
        evidence_count = int(math.floor(float(raw_count) + 0.5))
    evidence_count = max(0, evidence_count)
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

    Layered out-of-range policy (Hermes round-18 LOW)
    -------------------------------------------------
    Three call paths use different clamp policies by design:

      * High-traffic retrieval / lint paths clamp via
        ``extract_confidence_and_count`` and
        ``lint_confidence._clamp_confidence_pair`` so a single bad row
        cannot 500 a request.
      * The ``MemoryUnitDTO`` validator clamps + writes back via
        ``object.__setattr__`` for cross-field consistency
        (``confidence_variance`` is derived; the two must agree).
      * THIS function — the canonical formula — RAISES so a direct
        caller with bad input fails loudly at the computation site
        rather than silently returning garbage.

    The clamp helpers all use the same ``max(0.0, min(1.0, …))`` form,
    so the policy difference is "raise vs. clamp", not "different
    clamp". The cross-reference unit test in ``test_confidence.py``
    pins the formula equivalence between this module and the DTO
    validator.
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


def certainty_from_variance(variance: float) -> float:
    """Certainty factor in ``[0, 1]`` from a precomputed variance.

    Lets callers that already evaluated ``mean_and_variance`` reuse the
    result instead of re-running the closed form (Hermes round-12 LOW).
    """
    return 1.0 - variance / MAX_VARIANCE


def certainty(confidence: float, evidence_count: int) -> float:
    """Certainty factor in ``[0, 1]`` — used by the F22 F47 boost extension.

    ``certainty = 1 − variance / MAX_VARIANCE`` so cold-start (count=0) → 0
    (boost collapses to neutral) and well-evidenced → ~1 (full F47 lift).
    """
    _, variance = mean_and_variance(confidence, evidence_count)
    return certainty_from_variance(variance)
