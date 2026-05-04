"""Tests for the LongMemEval session-to-note adapter.

The pure ``build_note_payloads`` function is exercised with a unit test that
asserts every turn timestamp + session date round-trip through the
frontmatter the ingest pipeline reads. The full server-roundtrip test is
marked ``integration`` and deferred to an environment with Docker/Postgres.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from memex_eval.external.longmemeval_common import (
    LongMemEvalCategory,
    LongMemEvalQuestion,
    LongMemEvalSession,
    LongMemEvalTurn,
)
from memex_eval.external.longmemeval_ingest import build_note_payloads


def _sample_question() -> LongMemEvalQuestion:
    turns = [
        LongMemEvalTurn(
            role='user',
            content='When did we last talk about soccer?',
            timestamp=datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc),
        ),
        LongMemEvalTurn(
            role='assistant',
            content='Two weeks ago on Tuesday.',
            timestamp=datetime(2024, 1, 1, 8, 0, 5, tzinfo=timezone.utc),
        ),
    ]
    session = LongMemEvalSession(
        session_id='s1',
        session_date=datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc),
        turns=turns,
    )
    return LongMemEvalQuestion(
        question_id='q-temporal-001',
        category=LongMemEvalCategory.TEMPORAL_REASONING,
        is_abstention=False,
        question_text='When did the user last mention soccer?',
        answer='Two weeks ago.',
        sessions=[session],
    )


def test_build_note_payloads_one_per_session() -> None:
    q = _sample_question()
    vault_id = str(uuid4())
    payloads = build_note_payloads(q, variant='oracle', vault_id=vault_id)
    assert len(payloads) == 1
    p = payloads[0]
    assert p.vault_id == vault_id
    assert 'longmemeval' in p.tags
    assert 'variant:oracle' in p.tags
    assert 'question:q-temporal-001' in p.tags
    assert 'category:temporal-reasoning' in p.tags
    assert p.note_key == 'longmemeval-oracle-q-temporal-001-s1'


def test_build_note_payloads_preserves_timestamps_in_frontmatter() -> None:
    q = _sample_question()
    payloads = build_note_payloads(q, variant='oracle', vault_id=str(uuid4()))
    markdown = base64.b64decode(payloads[0].content).decode('utf-8')

    # The session timestamp must appear as publish_date in YAML frontmatter so
    # the core ingest pipeline propagates it to MemoryUnit.event_date.
    assert markdown.startswith('---\n')
    assert 'publish_date: 2024-01-01T08:00:00+00:00' in markdown
    # Every per-turn timestamp also appears verbatim in the dialogue body.
    assert '2024-01-01T08:00:00+00:00' in markdown
    assert '2024-01-01T08:00:05+00:00' in markdown
    # Turn content is preserved unchanged.
    assert 'When did we last talk about soccer?' in markdown
    assert 'Two weeks ago on Tuesday.' in markdown


@pytest.mark.integration
def test_ingest_roundtrip_event_date() -> None:
    """Full roundtrip: ingest one question's sessions and assert every
    session-turn timestamp matches the persisted ``MemoryUnit.mentioned_at``
    (or ``occurred_start`` for event-typed units).

    Gated behind ``integration`` so it runs under ``uv run pytest -m
    integration``, not by default.

    Blocker — explicit, not a silent skip: the full assertion requires
    the ``postgres_container`` / ``ensure_db_env_vars`` / ``async_client``
    fixtures defined in the repository-root ``tests/conftest.py``. Those
    fixtures import ``memex_core.server`` directly; ``packages/eval``
    does not depend on ``memex_core`` (only ``memex_common``) and adding
    that dependency is a scope change outside POC #13 (core is
    off-limits for this worktree).

    Options to unblock, in priority order:
      1. Relocate this test to ``tests/`` in the repo root, next to
         ``test_e2e_frontmatter_pipeline.py``, where the fixtures are
         already wired. ``build_note_payloads`` stays in
         ``packages/eval`` and is imported by the relocated test.
      2. Add ``memex-core`` as a dev-only dependency on ``packages/eval``
         and copy the relevant fixture block into
         ``packages/eval/tests/conftest.py``. Avoided here because it
         expands the eval package's runtime surface.

    We use ``importorskip`` so ``-m integration`` runs against the
    eval package surface a skip with a reason pointing at the blocker,
    while non-integration runs do not collect this test body at all
    (it is still collected by marker filtering but the importorskip
    call returns immediately when ``memex_core`` is missing).
    """
    pytest.importorskip(
        'memex_core',
        reason=(
            'Roundtrip assertion needs repo-root tests/conftest.py fixtures '
            '(postgres_container + async_client). Relocate this test to '
            'tests/ or add memex-core as a dev dep to packages/eval to unblock.'
        ),
    )
    # If memex_core IS importable (i.e. run from a full dev install),
    # we still need the repo-root fixtures to drive a real server. Those
    # are not currently discoverable from packages/eval/tests, so until
    # option (1) or (2) above is done, we surface a clear xfail rather
    # than pretending this covers the contract.
    pytest.xfail(
        'Integration fixtures (postgres_container, async_client) are not '
        'discoverable from packages/eval/tests — relocate this test to '
        'repo-root tests/ to fully exercise the roundtrip.'
    )
