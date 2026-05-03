"""Unit tests for the F25 / F25b write-time classifier surface.

After F25b the standalone classifier predictor is gone — intent + risk arrive
on each fact directly from the extraction LLM. What remains is:

- ``_coerce_intent`` / ``_coerce_risk`` default-on-fail coercion helpers.
- ``filter_safety_blocked`` — drops ``risk_class='safety'`` facts before
  persistence.
- The per-fact pydantic validators on ``RawFact`` that enforce the same
  default-on-fail behavior at parse time.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_core.memory.extraction.classifier import (
    DEFAULT_INTENT,
    DEFAULT_RISK,
    INTENT_VALUES,
    RISK_VALUES,
    _coerce_intent,
    _coerce_risk,
    filter_safety_blocked,
)
from memex_core.memory.extraction.models import ProcessedFact, RawFact


def _make_fact(text: str = 'a fact', context: str = '') -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type='world',
        embedding=[0.0] * 384,
        mentioned_at='2026-01-01T00:00:00+00:00',
        context=context,
        vault_id=uuid4(),
    )


class TestCoerce:
    @pytest.mark.parametrize('value', INTENT_VALUES)
    def test_intent_passthrough_for_valid_values(self, value: str) -> None:
        assert _coerce_intent(value) == value

    @pytest.mark.parametrize('value', RISK_VALUES)
    def test_risk_passthrough_for_valid_values(self, value: str) -> None:
        assert _coerce_risk(value) == value

    @pytest.mark.parametrize(
        'garbage', ['', 'unknown', None, 42, ['durable'], {'intent': 'durable'}]
    )
    def test_intent_defaults_on_garbage(self, garbage: object) -> None:
        assert _coerce_intent(garbage) == DEFAULT_INTENT

    @pytest.mark.parametrize('garbage', ['', 'unknown', None, 42, ['none']])
    def test_risk_defaults_on_garbage(self, garbage: object) -> None:
        assert _coerce_risk(garbage) == DEFAULT_RISK


class TestRawFactValidators:
    """The extraction LLM produces intent_class / risk_class on RawFact.
    Pydantic validators enforce default-on-fail so a malformed LLM output
    cannot crash the extraction pipeline.
    """

    @pytest.mark.parametrize('intent', list(INTENT_VALUES))
    def test_valid_intent_passes_through(self, intent: str) -> None:
        rf = RawFact(what='x', fact_type='world', intent_class=intent, risk_class='none')
        assert rf.intent_class == intent

    @pytest.mark.parametrize('risk', list(RISK_VALUES))
    def test_valid_risk_passes_through(self, risk: str) -> None:
        rf = RawFact(what='x', fact_type='world', intent_class='durable', risk_class=risk)
        assert rf.risk_class == risk

    def test_invalid_intent_falls_back_to_default(self) -> None:
        rf = RawFact(what='x', fact_type='world', intent_class='forever', risk_class='none')  # type: ignore[arg-type]
        assert rf.intent_class == DEFAULT_INTENT

    def test_invalid_risk_falls_back_to_default(self) -> None:
        rf = RawFact(
            what='x',
            fact_type='world',
            intent_class='durable',
            risk_class='very-bad',  # type: ignore[arg-type]
        )
        assert rf.risk_class == DEFAULT_RISK

    def test_omitted_classification_takes_field_defaults(self) -> None:
        rf = RawFact(what='x', fact_type='world')
        assert rf.intent_class == DEFAULT_INTENT
        assert rf.risk_class == DEFAULT_RISK


class TestFilterSafetyBlocked:
    def test_drops_safety_facts(self) -> None:
        facts = [
            _make_fact(text='ok 1'),
            _make_fact(text='blocked'),
            _make_fact(text='ok 2'),
        ]
        facts[1].risk_class = 'safety'

        kept = filter_safety_blocked(facts)

        assert len(kept) == 2
        assert all(f.risk_class != 'safety' for f in kept)
        assert kept[0].fact_text == 'ok 1'
        assert kept[1].fact_text == 'ok 2'

    def test_returns_all_when_nothing_blocked(self) -> None:
        facts = [_make_fact(text=f'ok {i}') for i in range(3)]
        kept = filter_safety_blocked(facts)
        assert len(kept) == 3

    def test_empty_input(self) -> None:
        assert filter_safety_blocked([]) == []

    def test_all_blocked(self) -> None:
        facts = [_make_fact(text=f'blocked {i}') for i in range(2)]
        for f in facts:
            f.risk_class = 'safety'
        assert filter_safety_blocked(facts) == []
