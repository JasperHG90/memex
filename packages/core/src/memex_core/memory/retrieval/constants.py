"""Decay-boost constants shared between the SQL FSFM clause and the reranker.

Single source of truth for stability semantics. Numeric values flow into SQL
via parameter binding (asyncpg ``$N``), never f-string interpolation. If the
unit convention changes (days -> hours), ``STABILITY_SECONDS_PER_DAY`` flips
in this one place and both paths stay in lockstep.
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
