"""Mint FSRS reference vectors by driving py-fsrs Scheduler.review_card.

Output: reference_vectors.json with provenance metadata + hash. Future
re-runs can detect drift (py-fsrs version bump, weight change, formula
patch) by hash comparison.

Usage:
    .venv-dev-ws-revisit/bin/python regenerate_reference_vectors.py
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy
from fsrs import Card, Rating, Scheduler

OUR_WEIGHTS = (
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
OUR_RETENTION = 0.9
OUR_MAXIMUM_INTERVAL = 36500
DETERMINISTIC_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
GAP_DAYS = 1.0

QUALITY_SEQUENCES: list[tuple[str, list[str]]] = [
    ('first_again', ['Again']),
    ('first_hard', ['Hard']),
    ('first_good', ['Good']),
    ('first_easy', ['Easy']),
    ('again_again', ['Again', 'Again']),
    ('again_good', ['Again', 'Good']),
    ('hard_good', ['Hard', 'Good']),
    ('good_good', ['Good', 'Good']),
    ('good_easy', ['Good', 'Easy']),
    ('easy_easy', ['Easy', 'Easy']),
    ('good_again', ['Good', 'Again']),
    ('easy_again', ['Easy', 'Again']),
    ('hard_hard_hard', ['Hard', 'Hard', 'Hard']),
    ('good_good_good', ['Good', 'Good', 'Good']),
    ('easy_easy_easy', ['Easy', 'Easy', 'Easy']),
    ('again_again_good', ['Again', 'Again', 'Good']),
    ('good_good_again', ['Good', 'Good', 'Again']),
    ('good_again_good', ['Good', 'Again', 'Good']),
    ('easy_again_easy', ['Easy', 'Again', 'Easy']),
    ('hard_good_easy_good', ['Hard', 'Good', 'Easy', 'Good']),
    ('again_hard_good_easy', ['Again', 'Hard', 'Good', 'Easy']),
    ('good_good_good_good', ['Good', 'Good', 'Good', 'Good']),
    ('easy_easy_easy_easy', ['Easy', 'Easy', 'Easy', 'Easy']),
    ('mixed_5', ['Good', 'Easy', 'Hard', 'Good', 'Good']),
    ('mixed_5b', ['Hard', 'Hard', 'Good', 'Easy', 'Good']),
    ('lapse_recover', ['Good', 'Good', 'Again', 'Good', 'Good']),
    ('long_easy_chain', ['Easy', 'Easy', 'Easy', 'Easy', 'Easy', 'Easy']),
    ('long_good_chain', ['Good', 'Good', 'Good', 'Good', 'Good', 'Good']),
    ('long_hard_chain', ['Hard', 'Hard', 'Hard', 'Hard', 'Hard', 'Hard']),
    ('long_again_chain', ['Again', 'Again', 'Again', 'Again']),
    ('mixed_long', ['Good', 'Easy', 'Good', 'Hard', 'Good', 'Easy', 'Good']),
    ('recovery_chain', ['Again', 'Good', 'Good', 'Easy', 'Easy']),
    ('hard_recovery', ['Hard', 'Good', 'Good', 'Easy']),
    ('easy_then_hard', ['Easy', 'Easy', 'Hard']),
    ('alternating', ['Good', 'Again', 'Good', 'Again', 'Good']),
]


def _rating(name: str) -> Rating:
    return getattr(Rating, name)


def _build_scheduler() -> Scheduler:
    return Scheduler(
        parameters=OUR_WEIGHTS,
        desired_retention=OUR_RETENTION,
        learning_steps=(),
        relearning_steps=(),
        maximum_interval=OUR_MAXIMUM_INTERVAL,
        enable_fuzzing=False,
    )


def _drive_sequence(qualities: list[str]) -> list[dict]:
    scheduler = _build_scheduler()
    card = Card()
    card.due = DETERMINISTIC_NOW
    steps: list[dict] = []
    review_at = DETERMINISTIC_NOW
    for idx, q in enumerate(qualities):
        rating = _rating(q)
        card, _ = scheduler.review_card(card, rating, review_at)
        interval_days = (card.due - review_at).days
        steps.append(
            {
                'step_index': idx,
                'quality': q,
                'review_at_iso': review_at.isoformat(),
                'next_review_at_iso': card.due.isoformat(),
                'interval_days': interval_days,
                'stability': card.stability,
                'difficulty': card.difficulty,
            }
        )
        review_at = card.due + timedelta(days=GAP_DAYS)
    return steps


def _edge_cases() -> list[dict]:
    return [
        {
            'case_id': 'edge_clock_skew_now_before_last',
            'description': 'now < last_review (clock skew); algorithm should not crash; '
            'elapsed_days clamps to 0.',
            'qualities': ['Good', 'Good'],
            'note': 'Tested by harness, not py-fsrs (py-fsrs accepts only forward time '
            'on review_card). Provenance: harness assertion only.',
        },
        {
            'case_id': 'edge_max_interval_cap',
            'description': 'After many Easy reviews, interval should saturate at '
            f'maximum_interval={OUR_MAXIMUM_INTERVAL}.',
            'qualities': ['Easy'] * 30,
            'note': 'Drives full sequence through py-fsrs and harness; both should '
            'cap at maximum_interval.',
        },
    ]


def main() -> int:
    sequences: list[dict] = []
    for case_id, qualities in QUALITY_SEQUENCES:
        sequences.append(
            {
                'case_id': case_id,
                'qualities': qualities,
                'steps': _drive_sequence(qualities),
            }
        )

    for edge in _edge_cases():
        edge_steps = _drive_sequence(edge['qualities'])
        sequences.append({**edge, 'steps': edge_steps})

    payload = {
        'schema_version': 1,
        'generation': {
            'py_fsrs_version': importlib.metadata.version('fsrs'),
            'numpy_version': numpy.__version__,
            'python_version': sys.version,
            'deterministic_now_iso': DETERMINISTIC_NOW.isoformat(),
            'gap_days_between_reviews': GAP_DAYS,
            'weights': list(OUR_WEIGHTS),
            'desired_retention': OUR_RETENTION,
            'maximum_interval': OUR_MAXIMUM_INTERVAL,
            'enable_fuzzing': False,
            'learning_steps': [],
            'relearning_steps': [],
            'note': (
                'learning_steps and relearning_steps are intentionally empty: '
                'F20 spec (RFC-014 §"FSRS implementation") goes straight to FSRS '
                'stability scheduling on first review and skips the Anki Learning/'
                'Relearning short-term-steps UX flow. Setting these to () forces '
                'py-fsrs to use the FSRS init/update formulas for all qualities.'
            ),
        },
        'sequences': sequences,
    }

    body = json.dumps(payload, indent=2, sort_keys=True).encode()
    payload['vectors_sha256'] = hashlib.sha256(body).hexdigest()

    out_path = Path(__file__).parent / 'reference_vectors.json'
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')

    total_steps = sum(len(seq['steps']) for seq in sequences)
    print(f'wrote {out_path.relative_to(Path(__file__).parent.parent.parent.parent.parent)}')
    print(f'  sequences: {len(sequences)}')
    print(f'  total parity steps: {total_steps}')
    print(f'  vectors_sha256: {payload["vectors_sha256"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
