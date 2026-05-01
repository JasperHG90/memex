"""F20 revisitation scheduler — FSRS-5 thin wrapper over py-fsrs 4.1.2.

`py-fsrs==4.1.2` implements FSRS-5 (19 weights), not FSRS-4.5 — the pip
version is unrelated to the algorithm version. py-fsrs v3.0.0 (2024-08-22)
moved from FSRS-4.5 to FSRS-5; v2.5.1 was the last FSRS-4.5 release.
Verified at `.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/
003-f20-fsrs-parity/paper-cross-check.md`.

This module is intentionally a thin adapter — algorithm code lives in the
upstream `fsrs` package. We expose a stable Memex-shaped surface
(`Quality`, `UnitState`, `schedule`) so callers in `services/revisitation.py`
and the scheduler tick are decoupled from the upstream type names.

Determinism: `Scheduler` is constructed with `enable_fuzzing=False` so the
next-review datetime is reproducible for a given (state, quality, now).
`learning_steps=()` and `relearning_steps=()` skip the Anki-flow
Learning/Relearning short-circuit — Memex units transition directly to the
Review state on first review (consistent with the F20 spec, RFC-014:170).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum

from fsrs import Card, Rating, Scheduler, State


class Quality(IntEnum):
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


_QUALITY_TO_RATING: dict[Quality, Rating] = {
    Quality.AGAIN: Rating.Again,
    Quality.HARD: Rating.Hard,
    Quality.GOOD: Rating.Good,
    Quality.EASY: Rating.Easy,
}


@dataclass
class UnitState:
    stability: float | None = None
    difficulty: float | None = None
    last_review: datetime | None = None


_SCHEDULER = Scheduler(
    learning_steps=(),
    relearning_steps=(),
    enable_fuzzing=False,
)


def schedule(
    state: UnitState | None,
    quality: Quality,
    now: datetime,
) -> tuple[datetime, int, float, float]:
    """Compute (next_review_at, interval_days, new_stability, new_difficulty).

    First review: state is None or stability is None — py-fsrs initializes a
    fresh Card and applies the FSRS-5 init formulas.
    Subsequent review: prior stability / difficulty / last_review are
    reconstituted onto a Card and `review_card` advances the schedule.

    Returns:
      next_review_at — UTC datetime, integer days from `now`
      interval_days — int, capped by py-fsrs `maximum_interval` (default 36500)
      new_stability — float
      new_difficulty — float (returned for caller persistence + audit; not
        re-derived elsewhere)
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if state is None or state.stability is None or state.difficulty is None:
        card = Card()
    else:
        last = state.last_review
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        card = Card(
            state=State.Review,
            stability=state.stability,
            difficulty=state.difficulty,
            due=last or now,
            last_review=last,
        )

    rating = _QUALITY_TO_RATING[quality]
    new_card, _review_log = _SCHEDULER.review_card(card, rating, review_datetime=now)

    next_review_at = new_card.due
    if next_review_at.tzinfo is None:
        next_review_at = next_review_at.replace(tzinfo=timezone.utc)
    interval_days = max(1, (next_review_at.date() - now.date()).days)
    return next_review_at, interval_days, float(new_card.stability), float(new_card.difficulty)
