"""FSRS-5 revisitation scheduler — thin wrapper over py-fsrs.

py-fsrs 4.1.2 implements FSRS-5 (19 weights), not FSRS-4.5. This module
exposes a stable Memex surface (Quality, UnitState, schedule) decoupled
from upstream type names. Scheduler uses enable_fuzzing=False for deterministic
next-review datetimes. learning_steps=() and relearning_steps=() skip
Anki-flow Learning/Relearning — units transition directly to Review.
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

    First review: state=None/stability=None -> fresh Card with FSRS-5 init.
    Subsequent: reconstitutes prior state onto Card and calls review_card.
    interval_days capped by py-fsrs maximum_interval (default 36500).
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
