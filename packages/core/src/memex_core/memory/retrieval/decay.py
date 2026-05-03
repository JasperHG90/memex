"""F11 — FSFM-lite decay boost composed at the reranker.

Closed form (mirrors F1c's compute_mw_boost in services/outcomes.py):

    boost = 1.0 + decay_alpha * (importance * exp(-elapsed_days / stability) - 0.5)

NULL-handling contract is a *split guard*, not a single early-return — each
NULL input maps to a different semantic:

  - importance is None   -> 1.0 (no signal to drive a boost)
  - last_outcome_at None -> 1.0 (no temporal anchor for decay)
  - stability is None    -> decay_term = 1.0 (the stability -> infinity
    limit; permanent units get importance-lifted, NOT decay-suppressed)

Permanent units (stability NULL, importance 1.0) at decay_alpha=0.3 therefore
return boost = 1.15 — a durable importance-based lift rather than a silent
exemption from the composition.

Synthetic defaults are explicitly forbidden for ``importance`` and
``last_outcome_at`` — that would silently boost or penalise truly
unclassified / never-touched units. The ``stability=None -> infinity``
mapping is the only synthetic value, and it's mathematically the
closed-form limit, not a guess.

``now`` is an injected parameter — never ``datetime.now(...)`` inside the
function — so unit tests pin time deterministically and the reranker
shares one timestamp across every per-unit boost in a query.
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
    """Compute the F11 decay boost for a single memory unit.

    Returns 1.0 (neutral) when ``importance`` or ``last_outcome_at`` is None.
    When ``stability`` is None the decay term collapses to 1.0 (permanent
    unit -> importance-lifted, not decay-suppressed).
    """
    if unit.importance is None or unit.last_outcome_at is None:
        return 1.0
    if unit.stability is None:
        decay_term = 1.0
    else:
        elapsed_days = (now - unit.last_outcome_at).total_seconds() / STABILITY_SECONDS_PER_DAY
        decay_term = math.exp(-elapsed_days / unit.stability)
    return 1.0 + decay_alpha * (unit.importance * decay_term - 0.5)
