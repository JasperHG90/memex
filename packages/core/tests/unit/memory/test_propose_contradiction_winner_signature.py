"""Unit tests for the ProposeContradictionWinner DSPy signature.

Covers:
- Field-set shape (inputs / outputs).
- Confidence clamping via @field_validator.
- Literal rejection on winner_id / loser_id / action.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex_core.memory.lint_llm.signatures import ProposeContradictionWinner


def _valid_kwargs(**overrides):
    base = {
        'unit_a_text': 'A',
        'unit_b_text': 'B',
        'unit_a_created_at': '2026-01-01T00:00:00+00:00',
        'unit_b_created_at': '2026-02-01T00:00:00+00:00',
        'unit_a_source_credibility': 0.7,
        'unit_b_source_credibility': 0.4,
        'unit_a_source_authority': 'official-doc',
        'unit_b_source_authority': 'chat-log',
        'fsfm_evidence': {'flag_reason': 'low_credibility_contradiction_only'},
        'winner_id': 'unit_a',
        'loser_id': 'unit_b',
        'rationale': 'higher authority, more recent',
        'confidence': 0.8,
        'action': 'mark_loser_stale',
    }
    base.update(overrides)
    return base


def test_signature_has_expected_fields():
    fields = set(ProposeContradictionWinner.model_fields)
    assert {
        'unit_a_text',
        'unit_b_text',
        'unit_a_created_at',
        'unit_b_created_at',
        'unit_a_source_credibility',
        'unit_b_source_credibility',
        'unit_a_source_authority',
        'unit_b_source_authority',
        'fsfm_evidence',
        'winner_id',
        'loser_id',
        'rationale',
        'confidence',
        'action',
    }.issubset(fields)


def test_confidence_clamped_to_unit_interval_above():
    inst = ProposeContradictionWinner(**_valid_kwargs(confidence=1.7))
    assert inst.confidence == 1.0


def test_confidence_clamped_to_unit_interval_below():
    inst = ProposeContradictionWinner(**_valid_kwargs(confidence=-0.5))
    assert inst.confidence == 0.0


def test_confidence_non_numeric_falls_back_to_zero():
    inst = ProposeContradictionWinner(**_valid_kwargs(confidence='not-a-number'))
    assert inst.confidence == 0.0


def test_winner_id_rejects_unknown_literal():
    with pytest.raises(ValidationError):
        ProposeContradictionWinner(**_valid_kwargs(winner_id='unit_c'))


def test_loser_id_rejects_unknown_literal():
    with pytest.raises(ValidationError):
        ProposeContradictionWinner(**_valid_kwargs(loser_id='unit_c'))


def test_action_rejects_unknown_literal():
    with pytest.raises(ValidationError):
        ProposeContradictionWinner(**_valid_kwargs(action='delete_everything'))


def test_inconclusive_literals_accepted():
    inst = ProposeContradictionWinner(
        **_valid_kwargs(winner_id='inconclusive', loser_id='none', action='inconclusive')
    )
    assert inst.winner_id == 'inconclusive'
    assert inst.loser_id == 'none'
    assert inst.action == 'inconclusive'
