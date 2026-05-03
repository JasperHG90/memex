"""Constants shared between F40's FSFM SQL clause and F11's reranker boost.

Single source of truth — both code paths import from here so there's exactly
one definition of stability semantics. Numeric values flow into SQL via
parameter binding (asyncpg ``$N`` placeholders), never f-string interpolation.

If ``stability``'s unit convention ever changes (e.g., days -> hours), the
``STABILITY_SECONDS_PER_DAY`` divisor flips in this one place and both
F40's SQL builder and F11's Python boost stay in lockstep automatically.
"""

from __future__ import annotations

STABILITY_SECONDS_PER_DAY: float = 86400.0

STABILITY_THRESHOLD: float = 0.10

INTENT_STABILITY_DAYS: dict[str, float | None] = {
    'permanent': None,
    'durable': 180.0,
    'ephemeral': 14.0,
}

INTENT_IMPORTANCE: dict[str, float] = {
    'permanent': 1.0,
    'durable': 0.7,
    'ephemeral': 0.3,
}
