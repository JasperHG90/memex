"""Decay boost composed at the reranker (mirrors compute_mw_boost in services/outcomes.py).

    boost = 1.0 + decay_alpha * (importance * exp(-elapsed_days / stability) - 0.5)

NULL contract (split guard, not single early-return):
  - importance None      -> 1.0 (no signal)
  - last_outcome_at None -> 1.0 (no temporal anchor)
  - stability None       -> decay_term=1.0 (permanent: importance-lifted, not suppressed)

Permanent units (stability=None, importance=1.0) at decay_alpha=0.3 return 1.15.
Synthetic defaults forbidden for importance/last_outcome_at; stability=None maps
to the closed-form infinity limit.

``now`` is injected (never datetime.now() inside) for deterministic tests.
elapsed_days clamped >=0 to handle clock skew (monotonicity contract).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol

from memex_core.memory.retrieval.constants import STABILITY_SECONDS_PER_DAY


class DecayInputs(Protocol):
    importance: float | None
    stability: float | None
    last_outcome_at: datetime | None


def compute_decay_boost(unit: DecayInputs, decay_alpha: float, now: datetime) -> float:
    """Compute decay boost for a single unit. Returns 1.0 when importance or
    last_outcome_at is None. stability=None -> decay_term=1.0 (permanent)."""
    if unit.importance is None or unit.last_outcome_at is None:
        return 1.0
    if unit.stability is None:
        decay_term = 1.0
    else:
        elapsed_days = max(
            0.0, (now - unit.last_outcome_at).total_seconds() / STABILITY_SECONDS_PER_DAY
        )
        decay_term = math.exp(-elapsed_days / unit.stability)
    return 1.0 + decay_alpha * (unit.importance * decay_term - 0.5)
