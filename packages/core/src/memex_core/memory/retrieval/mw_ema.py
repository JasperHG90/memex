"""EMA-decayed Memory Worth score computation.

Applies exponential decay to the accumulating Bernoulli counters at read time
so stale evidence fades toward the Beta(1,1) prior mean of 0.5.

Closed form:
    elapsed_days  = (now - last_outcome_at) / 86400
    decay_factor  = exp(-elapsed_days * ln(2) / half_life_days)
    success_decay = success * decay_factor
    failure_decay = failure * decay_factor
    mw_ema_score  = (success_decay + 1) / (success_decay + failure_decay + 2)

The posterior is Beta(success_decay + 1, failure_decay + 1) — the same
Beta(1, 1) uniform prior as the stationary compute_mw_score. Cold-start
(last_outcome_at is None) returns 0.5 (the prior mean).
"""

from __future__ import annotations

import math
from datetime import datetime


def compute_mw_ema_score(
    success: int,
    failure: int,
    last_outcome_at: datetime | None,
    half_life_days: float,
    now: datetime,
) -> float:
    if half_life_days <= 0:
        raise ValueError(f'half_life_days must be positive, got {half_life_days}')
    if last_outcome_at is None:
        return 0.5

    elapsed_seconds = (now - last_outcome_at).total_seconds()
    if elapsed_seconds < 0:
        elapsed_seconds = 0.0
    elapsed_days = elapsed_seconds / 86400.0
    decay_factor = math.exp(-elapsed_days * math.log(2) / half_life_days)
    success_decay = success * decay_factor
    failure_decay = failure * decay_factor
    return (success_decay + 1.0) / (success_decay + failure_decay + 2.0)
