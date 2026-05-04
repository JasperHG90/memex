"""Polarity-discriminating NLI wrapper.

Augments the surprise gate with a three-way NLI signal so polarity-inverting
unit/peer pairs (POC-002 result.md) clear the gate even when MiniLM-L12
cosine surprise alone keeps them below the surprise threshold.

The composition is OR'd: a unit/peer pair clears the gate when EITHER

  cosine_surprise >= surprise_threshold

OR

  nli_contradiction_prob >= polarity_threshold (default 0.6)

The NLI invocation is gated to *only* run when cosine surprise is below the
threshold — saves the per-pair NLI call when cosine alone already cleared.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from memex_core.memory.lint_llm.types import PolarityLabel, PolarityResult
from memex_core.memory.models.protocols import NLIClassifierModel

logger = logging.getLogger('memex.core.memory.lint_llm.polarity')

DEFAULT_POLARITY_THRESHOLD: float = 0.6


@dataclass
class PolarityClassifyOutcome:
    """Discriminated outcome of a single ``PolarityClassifier.classify_pair`` call.

    The orchestrator inspects this to distinguish the three terminal states a
    classify call can land in: a real NLI result, a soft rate-limit fallback,
    or a model-side failure that was suppressed (logged + degraded). Splitting
    the latter two lets observability tell "the limiter said no" from "the
    model crashed and we silently degraded" — a Hermes round-7 finding.
    """

    result: PolarityResult | None = None
    rate_limited: bool = False
    model_failed: bool = False


class PolarityRateLimiter:
    """Per-vault hourly cap on NLI invocations.

    In-memory sliding window — the lint scheduler is a single-leader process
    (Postgres advisory lock) so a process-local counter is sufficient. The cap
    is a soft circuit-breaker; over-cap calls return ``False`` and the gate
    falls back to cosine-only behaviour for that pair.

    ``admit`` reserves a slot under the lock so concurrent classify calls cannot
    over-commit the cap; ``release`` removes the most recently reserved slot,
    used to refund the reservation when the downstream model invocation fails
    (Hermes round-7 MED 2 — burning a slot on a model crash silently degrades
    the per-vault reliability budget).
    """

    def __init__(self, max_per_vault_per_hour: int | None) -> None:
        self.max_per_hour = max_per_vault_per_hour
        self._buckets: dict[UUID, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def admit(self, vault_id: UUID) -> bool:
        if self.max_per_hour is None:
            return True
        now = time.monotonic()
        cutoff = now - 3600.0
        async with self._lock:
            bucket = self._buckets[vault_id]
            bucket[:] = [t for t in bucket if t >= cutoff]
            if len(bucket) >= self.max_per_hour:
                return False
            bucket.append(now)
            return True

    async def release(self, vault_id: UUID) -> None:
        """Refund the most recently reserved slot for ``vault_id``.

        No-op when the limiter is unbounded or the bucket is already empty.
        Called by ``PolarityClassifier`` only when the model invocation raised
        or returned malformed output — successful calls (including labels of
        ``neutral``) keep the slot consumed.
        """
        if self.max_per_hour is None:
            return
        async with self._lock:
            bucket = self._buckets[vault_id]
            if bucket:
                bucket.pop()

    async def used(self, vault_id: UUID) -> int:
        if self.max_per_hour is None:
            return 0
        now = time.monotonic()
        cutoff = now - 3600.0
        async with self._lock:
            bucket = self._buckets[vault_id]
            bucket[:] = [t for t in bucket if t >= cutoff]
            return len(bucket)


class PolarityClassifier:
    """Per-pair NLI invocation + gate-aware composition.

    Uses ``NLIClassifierModel`` for the inference call and ``PolarityRateLimiter``
    for the per-vault hourly cap (None = unlimited). The argmax label and
    raw probabilities are returned in a :class:`PolarityResult` so the LLM
    check can carry the label through as a hint.
    """

    def __init__(
        self,
        model: NLIClassifierModel,
        *,
        polarity_threshold: float = DEFAULT_POLARITY_THRESHOLD,
        rate_limiter: PolarityRateLimiter | None = None,
    ) -> None:
        if not 0.0 <= polarity_threshold <= 1.0:
            raise ValueError(f'polarity_threshold out of range: {polarity_threshold}')
        self.model = model
        self.polarity_threshold = polarity_threshold
        self.rate_limiter = rate_limiter or PolarityRateLimiter(max_per_vault_per_hour=None)

    async def classify_pair(
        self,
        premise: str,
        hypothesis: str,
        *,
        vault_id: UUID,
    ) -> PolarityClassifyOutcome:
        """Run NLI on a single pair, respecting the per-vault rate limit.

        Returns a :class:`PolarityClassifyOutcome` with exactly one of:

        * ``result`` set — the model returned a well-formed three-way
          probability dict (any label, including ``neutral``). The slot stays
          consumed.
        * ``rate_limited=True`` — the per-vault cap rejected the reservation
          before the model was invoked. No slot consumed.
        * ``model_failed=True`` — the model raised, returned malformed
          output, or otherwise crashed mid-call. The reserved slot is
          refunded so a hot-failing model cannot silently exhaust the
          per-vault reliability budget (Hermes round-7 MED 2). Failures are
          logged so they are distinguishable from "model said neutral" in
          scheduler logs, rather than propagating up through ``maybe_run``
          → ``tick``'s generic exception handler with a non-specific
          traceback.
        """
        admitted = await self.rate_limiter.admit(vault_id)
        if not admitted:
            logger.warning(
                'NLI rate-limit exhausted for vault %s — falling back to cosine-only',
                vault_id,
            )
            return PolarityClassifyOutcome(rate_limited=True)

        try:
            probs = await self.model.classify(premise=premise, hypothesis=hypothesis)
            result = PolarityResult(
                label=_argmax_label(probs),
                contradiction_prob=float(probs.get('contradiction', 0.0)),
                entailment_prob=float(probs.get('entailment', 0.0)),
                neutral_prob=float(probs.get('neutral', 0.0)),
            )
            return PolarityClassifyOutcome(result=result)
        except Exception:
            logger.exception(
                'NLI classify failed for vault %s — falling back to cosine-only',
                vault_id,
            )
            await self.rate_limiter.release(vault_id)
            return PolarityClassifyOutcome(model_failed=True)


def _argmax_label(probs: dict[str, float]) -> PolarityLabel:
    """Return the argmax three-way label.

    Raises ``ValueError`` on empty input — an empty ``probs`` dict almost
    certainly indicates a malformed model output / ONNX session failure
    upstream, and silently masking it as ``NEUTRAL`` would leave operators
    unable to distinguish "the model said neutral" from "the model produced
    no output."
    """
    if not probs:
        raise ValueError(
            'Cannot derive argmax label: NLI classifier returned empty '
            'probability dict (likely malformed model output).'
        )
    label = max(probs, key=lambda k: probs[k])
    return PolarityLabel(label)


def gate_passes(
    cosine_surprise: float,
    polarity_contra_prob: float | None,
    *,
    surprise_threshold: float,
    polarity_threshold: float = DEFAULT_POLARITY_THRESHOLD,
) -> bool:
    """OR-combine the cosine gate with the polarity gate.

    The contract is symmetric: the pair clears the gate when EITHER signal
    crosses its respective threshold. The caller is expected to pass
    ``polarity_contra_prob=None`` when cosine surprise already cleared so the
    NLI invocation is skipped (cheap pre-filter).
    """
    if cosine_surprise >= surprise_threshold:
        return True
    if polarity_contra_prob is not None and polarity_contra_prob >= polarity_threshold:
        return True
    return False
