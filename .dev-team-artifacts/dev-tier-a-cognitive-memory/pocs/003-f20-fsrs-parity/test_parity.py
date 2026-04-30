"""POC-F20 — FSRS-4.5 reference parity harness.

Verifies the vendored FSRS-4.5 port at `harness/schedule.py` reproduces
py-fsrs (`Scheduler.review_card`) outputs within tolerance over the
reference vectors at `reference_vectors.json`.

Tolerances (RFC-014's calibrated bound):
- stability: abs(diff) <= 1e-4
- interval (integer days): exact match (py-fsrs and our port both round
  via `_next_interval`, so the integer-day tolerance is effectively zero;
  we still assert a 1-day soft bound for documentation)
- next_review_at: exact match (derived deterministically from `now +
  interval`, both implementations use the same `now`)

The POC harness lives outside `tests/` and is run only when validating
the POC, not on every PR. Production parity test for #24 ports a
~20-case subset to `tests/unit/test_fsrs_parity.py` for default CI; full
80+ stays opt-in via `@pytest.mark.slow`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.schedule import (
    FSRSParams,
    Quality,
    UnitState,
    schedule,
)

REF_PATH = Path(__file__).parent / 'reference_vectors.json'
REF: dict = json.loads(REF_PATH.read_text())

# SHA256 of reference_vectors.json file bytes; py-fsrs v4.x. Pin guards against an
# accidental edit OR an unreviewed regenerate; only update after a deliberate review.
REFERENCE_VECTORS_SHA256 = '0a00dda35aa0661681cd439ff39406a0308175df877aba4d6d28ce737fe2069a'

STABILITY_TOLERANCE = 1e-4
INTERVAL_TOLERANCE_DAYS = 1


def _quality(name: str) -> Quality:
    return Quality[name.upper()]


def _params_from_ref() -> FSRSParams:
    g = REF['generation']
    return FSRSParams(
        w=tuple(g['weights']),
        desired_retention=g['desired_retention'],
        maximum_interval=g['maximum_interval'],
    )


def test_reference_vectors_file_bytes_match_pinned_sha256() -> None:
    """File-bytes sha256 must match the module-level pin. A regenerate
    that updates the embedded body hash inside the JSON would still flip
    the file bytes and trip this test, forcing a deliberate constant
    update. Catches both accidental edits and unreviewed regenerations."""
    actual = hashlib.sha256(REF_PATH.read_bytes()).hexdigest()
    assert actual == REFERENCE_VECTORS_SHA256, (
        f'reference_vectors.json bytes drifted: '
        f'expected {REFERENCE_VECTORS_SHA256}, got {actual}. '
        f'Regenerate via regenerate_reference_vectors.py, then update '
        f'REFERENCE_VECTORS_SHA256 in this module only after a deliberate review.'
    )


def test_reference_vectors_internal_hash_consistent() -> None:
    """The reference JSON's embedded vectors_sha256 must match a re-hash
    of its deterministic body — proves the file's self-claim is honest."""
    payload = json.loads(REF_PATH.read_text())
    stored = payload.pop('vectors_sha256')
    body = json.dumps(payload, indent=2, sort_keys=True).encode()
    assert hashlib.sha256(body).hexdigest() == stored, (
        'reference_vectors.json content drifted from stored hash; '
        're-run regenerate_reference_vectors.py and check provenance'
    )


def _flat_first_review_cases() -> list[tuple[str, str, dict]]:
    out: list[tuple[str, str, dict]] = []
    for seq in REF['sequences']:
        first = seq['steps'][0]
        out.append((seq['case_id'], first['quality'], first))
    return out


def _flat_subsequent_review_cases() -> list[tuple[str, list[dict]]]:
    out: list[tuple[str, list[dict]]] = []
    for seq in REF['sequences']:
        if len(seq['steps']) >= 2:
            out.append((seq['case_id'], seq['steps']))
    return out


@pytest.mark.parametrize(
    'case_id, quality, expected',
    _flat_first_review_cases(),
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_fsrs_first_review_parity(case_id: str, quality: str, expected: dict) -> None:
    params = _params_from_ref()
    review_at = datetime.fromisoformat(expected['review_at_iso'])
    next_review_at, interval, stability, _ = schedule(
        state=None,
        quality=_quality(quality),
        now=review_at,
        params=params,
    )
    assert abs(stability - expected['stability']) <= STABILITY_TOLERANCE, (
        f'{case_id}: stability {stability!r} vs expected {expected["stability"]!r} '
        f'(diff {stability - expected["stability"]!r})'
    )
    assert abs(interval - expected['interval_days']) <= INTERVAL_TOLERANCE_DAYS, (
        f'{case_id}: interval {interval} vs expected {expected["interval_days"]}'
    )
    assert next_review_at.isoformat() == expected['next_review_at_iso'], (
        f'{case_id}: next_review_at {next_review_at.isoformat()} '
        f'vs expected {expected["next_review_at_iso"]}'
    )


@pytest.mark.parametrize(
    'case_id, steps',
    _flat_subsequent_review_cases(),
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_fsrs_subsequent_review_parity(case_id: str, steps: list[dict]) -> None:
    params = _params_from_ref()
    state = UnitState()
    for idx, step in enumerate(steps):
        review_at = datetime.fromisoformat(step['review_at_iso'])
        next_review_at, interval, stability, difficulty = schedule(
            state=state if state.stability is not None else None,
            quality=_quality(step['quality']),
            now=review_at,
            params=params,
        )
        assert abs(stability - step['stability']) <= STABILITY_TOLERANCE, (
            f'{case_id} step {idx} ({step["quality"]}): stability {stability!r} '
            f'vs expected {step["stability"]!r} '
            f'(diff {stability - step["stability"]!r})'
        )
        assert abs(interval - step['interval_days']) <= INTERVAL_TOLERANCE_DAYS, (
            f'{case_id} step {idx} ({step["quality"]}): interval {interval} '
            f'vs expected {step["interval_days"]}'
        )
        assert next_review_at.isoformat() == step['next_review_at_iso'], (
            f'{case_id} step {idx} ({step["quality"]}): '
            f'next_review_at {next_review_at.isoformat()} '
            f'vs expected {step["next_review_at_iso"]}'
        )
        state = UnitState(
            stability=stability,
            difficulty=difficulty,
            last_review=review_at,
        )


def test_edge_clock_skew_now_before_last_does_not_crash() -> None:
    """Clock skew: a daily scheduler tick may observe `now < last_review`
    if the host clock drifts backward (NTP correction, container snapshot
    restore). FSRS must not produce negative intervals or crash on
    backward time; the port clamps `elapsed_days` to 0 so retrievability
    is treated as 1 (perfectly retained), and the next review still
    advances forward by `interval_days` from `now`."""
    params = _params_from_ref()
    last = datetime(2026, 6, 1, tzinfo=timezone.utc)
    state = UnitState(stability=10.0, difficulty=5.0, last_review=last)
    earlier = datetime(2026, 5, 1, tzinfo=timezone.utc)
    next_review_at, interval, stability, _ = schedule(state, Quality.GOOD, earlier, params)
    assert stability > 0
    assert interval >= 1
    assert next_review_at > earlier


def test_edge_max_interval_cap() -> None:
    """Long-horizon cap: `maximum_interval=36500` (100 years) is the
    spec-mandated upper bound on `next_review_at - now`. After enough
    Easy reviews, stability grows unboundedly large and `_next_interval`
    must saturate at the cap rather than scheduling reviews thousands of
    years out. Guards against floating-point overflow on `next_review_at
    = now + timedelta(days=interval)` and against runaway scheduling."""
    params = _params_from_ref()
    state = UnitState()
    review_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    capped = False
    for _ in range(40):
        next_review_at, interval, stability, difficulty = schedule(
            state=state if state.stability is not None else None,
            quality=Quality.EASY,
            now=review_at,
            params=params,
        )
        if interval == params.maximum_interval:
            capped = True
            break
        state = UnitState(stability=stability, difficulty=difficulty, last_review=review_at)
        review_at = next_review_at
    assert capped, 'interval should saturate at maximum_interval after enough Easy reviews'


def test_edge_first_review_again_initial_stability_floor() -> None:
    """Initial-stability floor: spec mandates `initial_stability =
    max(w[quality-1], 0.1)`. With py-fsrs's default `w[0]=0.40255` the
    floor doesn't bite, but if a future weight retraining drops `w[0]`
    below 0.1 (or a vault-tuned weight is set absurdly low), retrievability
    decay would explode at small stabilities. The floor prevents
    pathological intervals on the very first Again review and keeps the
    formula numerically stable."""
    p = FSRSParams(w=(0.05,) + FSRSParams().w[1:])
    _, _, stability, _ = schedule(None, Quality.AGAIN, datetime(2026, 1, 1, tzinfo=timezone.utc), p)
    assert stability == 0.1


def test_edge_difficulty_bounds_clamp() -> None:
    """Difficulty clamp: spec mandates `1.0 <= difficulty <= 10.0` at
    every step. A long Again chain pushes difficulty toward the upper
    bound; an Easy chain pushes it toward the lower bound. Without the
    clamp, `_next_difficulty` could go negative (breaking the FSRS-4.5
    `(11 - difficulty)` factor in `_next_recall_stability`) or exceed
    10 (compressing intervals to floor-1-day forever). Catches a
    refactor that drops the `min/max` bounds in `_next_difficulty`."""
    params = _params_from_ref()
    state = UnitState()
    review_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(10):
        _, _, stability, difficulty = schedule(
            state=state if state.stability is not None else None,
            quality=Quality.AGAIN,
            now=review_at,
            params=params,
        )
        assert 1.0 <= difficulty <= 10.0
        state = UnitState(stability=stability, difficulty=difficulty, last_review=review_at)
