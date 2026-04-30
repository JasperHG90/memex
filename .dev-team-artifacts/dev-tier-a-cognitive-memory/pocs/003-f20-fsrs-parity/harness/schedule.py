"""Vendored FSRS-4.5 scheduler — POC port.

Mirrors the formula structure of `py-fsrs==4.1.2` (FSRS-4.5 algorithm, see
`fsrs/fsrs.py:652-764` in that package). This port is what will land in
`packages/core/src/memex_core/memory/revisit.py` once #14 PASSES — keeping
algorithm code in one place and letting the parity harness verify our port
against py-fsrs as the gold standard.

Notable design choices:
- `enable_fuzzing` is hard-coded off. F20's scheduler is deterministic per
  spec (RFC-014 §"FSRS implementation"); fuzzing is an Anki UX feature that
  Memex does not need.
- Learning-steps short-circuit is omitted. Memex units skip the
  `Learning`/`Relearning` Anki-flow states; the first review writes
  stability/difficulty directly via the init formulas.
- Interval is rounded to int days (matching py-fsrs `_next_interval`),
  capped by `maximum_interval`.
- DECAY = -0.5 and FACTOR = 0.9**(1/-0.5) - 1 are FSRS-4.5 retrievability
  constants tied to a 0.9 default request-retention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum

DECAY: float = -0.5
FACTOR: float = 0.9 ** (1 / DECAY) - 1


class Quality(IntEnum):
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


@dataclass(frozen=True)
class FSRSParams:
    w: tuple[float, ...] = (
        0.40255,
        1.18385,
        3.173,
        15.69105,
        7.1949,
        0.5345,
        1.4604,
        0.0046,
        1.54575,
        0.1192,
        1.01925,
        1.9395,
        0.11,
        0.29605,
        2.2698,
        0.2315,
        2.9898,
        0.51655,
        0.6621,
    )
    desired_retention: float = 0.9
    maximum_interval: int = 36500


@dataclass
class UnitState:
    stability: float | None = None
    difficulty: float | None = None
    last_review: datetime | None = None


def _initial_stability(quality: Quality, p: FSRSParams) -> float:
    return max(p.w[quality - 1], 0.1)


def _initial_difficulty(quality: Quality, p: FSRSParams) -> float:
    d = p.w[4] - math.exp(p.w[5] * (quality - 1)) + 1
    return min(max(d, 1.0), 10.0)


def _next_difficulty(difficulty: float, quality: Quality, p: FSRSParams) -> float:
    delta = -(p.w[6] * (quality - 3))
    damped = (10.0 - difficulty) * delta / 9.0
    arg_2 = difficulty + damped
    arg_1 = _initial_difficulty(Quality.EASY, p)
    nd = p.w[7] * arg_1 + (1 - p.w[7]) * arg_2
    return min(max(nd, 1.0), 10.0)


def _retrievability(elapsed_days: float, stability: float) -> float:
    return (1 + FACTOR * elapsed_days / stability) ** DECAY


def _next_recall_stability(
    difficulty: float, stability: float, retrievability: float, quality: Quality, p: FSRSParams
) -> float:
    hard_penalty = p.w[15] if quality == Quality.HARD else 1.0
    easy_bonus = p.w[16] if quality == Quality.EASY else 1.0
    return stability * (
        1
        + math.exp(p.w[8])
        * (11 - difficulty)
        * math.pow(stability, -p.w[9])
        * (math.exp((1 - retrievability) * p.w[10]) - 1)
        * hard_penalty
        * easy_bonus
    )


def _next_forget_stability(
    difficulty: float, stability: float, retrievability: float, p: FSRSParams
) -> float:
    long_term = (
        p.w[11]
        * math.pow(difficulty, -p.w[12])
        * (math.pow(stability + 1, p.w[13]) - 1)
        * math.exp((1 - retrievability) * p.w[14])
    )
    short_term = stability / math.exp(p.w[17] * p.w[18])
    return min(long_term, short_term)


def _next_interval(stability: float, p: FSRSParams) -> int:
    raw = (stability / FACTOR) * (p.desired_retention ** (1 / DECAY) - 1)
    rounded = round(raw)
    return min(max(rounded, 1), p.maximum_interval)


def schedule(
    state: UnitState | None,
    quality: Quality,
    now: datetime,
    params: FSRSParams = FSRSParams(),
) -> tuple[datetime, int, float, float]:
    """Compute (next_review_at, interval_days, new_stability, new_difficulty).

    First review: state is None or stability is None. Use init formulas.
    Subsequent review: read prior stability/difficulty/last_review off state.

    Per F20 spec: returns the next-review datetime in UTC, the integer
    interval (rounded to days, capped by maximum_interval), the new
    stability, and the new difficulty. Caller persists stability + interval
    + next_review_at on MemoryUnit; difficulty is recomputed from
    (success_co_count, failure_co_count) at next review time so we don't
    need to store it. The POC port returns it for parity verification only.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if state is None or state.stability is None or state.difficulty is None:
        new_stability = _initial_stability(quality, params)
        new_difficulty = _initial_difficulty(quality, params)
    else:
        last = state.last_review
        if last is None:
            elapsed_days = 0.0
        else:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed_seconds = (now - last).total_seconds()
            elapsed_days = max(elapsed_seconds / 86400.0, 0.0)
        retrievability = _retrievability(elapsed_days, state.stability)
        if quality == Quality.AGAIN:
            new_stability = _next_forget_stability(
                state.difficulty, state.stability, retrievability, params
            )
        else:
            new_stability = _next_recall_stability(
                state.difficulty, state.stability, retrievability, quality, params
            )
        new_difficulty = _next_difficulty(state.difficulty, quality, params)

    interval_days = _next_interval(new_stability, params)
    next_review_at = now + timedelta(days=interval_days)
    return next_review_at, interval_days, new_stability, new_difficulty
