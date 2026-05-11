"""Unit tests for ``make_propose_contradiction_winner_check`` gating.

Focused on the confidence gate: a definitive (unit_a / unit_b) verdict
below ``min_confidence`` must be downgraded to ``inconclusive`` before
the finding is emitted, so the apply path cannot mutate user data on
the back of low-confidence LLM output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_core.memory.lint_llm.checks import make_propose_contradiction_winner_check


def _row(**kwargs):
    return SimpleNamespace(**kwargs)


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def _make_session(rows: list):
    captured: list = []

    async def execute(sql, params=None):
        captured.append((str(getattr(sql, 'text', sql)), params))
        idx = len(captured) - 1
        if idx < len(rows):
            return _Result(rows[idx])
        return _Result(None)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    return session


def _fsfm_row():
    return _row(
        finding_id=str(uuid4()),
        evidence={'flag_reason': 'low_credibility_contradiction_only'},
    )


def _peer_row():
    return _row(
        link_id=str(uuid4()),
        source_unit_id=str(uuid4()),
        weight=1.0,
        link_created_at=datetime.now(timezone.utc),
        source_text='Peer text',
        source_created_at=datetime.now(timezone.utc),
        source_confidence=0.7,
        source_mw=0.6,
        source_note_id=str(uuid4()),
        source_note_metadata={'authority': 'official-doc'},
    )


def _loser_row():
    return _row(
        unit_text='Audited unit text',
        unit_created_at=datetime.now(timezone.utc),
        unit_confidence=0.5,
        unit_mw=0.4,
        note_id=str(uuid4()),
        note_metadata={'authority': 'chat-log'},
    )


@pytest.mark.asyncio
async def test_low_confidence_definitive_verdict_is_downgraded_to_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
):
    """A confident-looking unit_a / unit_b verdict with confidence below
    the gate must be emitted as ``inconclusive`` so apply is blocked."""
    fake_prediction = SimpleNamespace(
        winner_id='unit_a',
        loser_id='unit_b',
        action='mark_loser_stale',
        confidence=0.2,
        rationale='not actually confident',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    session = _make_session([_fsfm_row(), None, _peer_row(), _loser_row()])
    check = make_propose_contradiction_winner_check(lm=object(), min_confidence=0.6)

    finding = await check(uuid4(), uuid4(), session, None)

    assert finding is not None
    assert finding.extra_evidence['winner_id'] == 'inconclusive'
    assert finding.extra_evidence['loser_id'] == 'none'
    assert finding.extra_evidence['action'] == 'inconclusive'
    assert finding.extra_evidence['winner_unit_id'] is None


@pytest.mark.asyncio
async def test_high_confidence_definitive_verdict_passes_through(
    monkeypatch: pytest.MonkeyPatch,
):
    """A high-confidence definitive verdict must be emitted as-is."""
    peer = _peer_row()
    fake_prediction = SimpleNamespace(
        winner_id='unit_a',
        loser_id='unit_b',
        action='mark_loser_stale',
        confidence=0.9,
        rationale='strong evidence',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    session = _make_session([_fsfm_row(), None, peer, _loser_row()])
    check = make_propose_contradiction_winner_check(lm=object(), min_confidence=0.6)

    finding = await check(uuid4(), uuid4(), session, None)

    assert finding is not None
    assert finding.extra_evidence['winner_id'] == 'unit_a'
    assert finding.extra_evidence['loser_id'] == 'unit_b'
    assert finding.extra_evidence['action'] == 'mark_loser_stale'
    assert finding.extra_evidence['winner_unit_id'] == peer.source_unit_id


@pytest.mark.asyncio
async def test_inconclusive_verdict_emitted_for_audit_trail(
    monkeypatch: pytest.MonkeyPatch,
):
    """An ``inconclusive`` verdict must still be emitted as a finding,
    regardless of confidence — the audit trail records that the LLM tried
    and the apply dispatcher treats it as a no-op."""
    fake_prediction = SimpleNamespace(
        winner_id='inconclusive',
        loser_id='none',
        action='inconclusive',
        confidence=0.95,
        rationale='cannot tell them apart',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    session = _make_session([_fsfm_row(), None, _peer_row(), _loser_row()])
    check = make_propose_contradiction_winner_check(lm=object(), min_confidence=0.6)

    finding = await check(uuid4(), uuid4(), session, None)

    assert finding is not None
    assert finding.extra_evidence['action'] == 'inconclusive'
    assert finding.extra_evidence['winner_unit_id'] is None


@pytest.mark.asyncio
async def test_inconclusive_winner_forces_inconclusive_action(
    monkeypatch: pytest.MonkeyPatch,
):
    """When the LLM returns winner_id='inconclusive' but a definitive
    action (e.g. mark_loser_stale), the emitted finding must normalize
    the action to ``inconclusive`` and loser_id to ``none`` so the apply
    path cannot run a definitive mutation with no real winner."""
    fake_prediction = SimpleNamespace(
        winner_id='inconclusive',
        loser_id='unit_b',
        action='mark_loser_stale',
        confidence=0.95,
        rationale='conflicting signals from LLM',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    session = _make_session([_fsfm_row(), None, _peer_row(), _loser_row()])
    check = make_propose_contradiction_winner_check(lm=object(), min_confidence=0.6)

    finding = await check(uuid4(), uuid4(), session, None)

    assert finding is not None
    assert finding.extra_evidence['action'] == 'inconclusive'
    assert finding.extra_evidence['winner_id'] == 'inconclusive'
    assert finding.extra_evidence['loser_id'] == 'none'
    assert finding.extra_evidence['winner_unit_id'] is None
