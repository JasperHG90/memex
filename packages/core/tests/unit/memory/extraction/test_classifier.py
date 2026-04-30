"""Unit tests for F25 write-time classifier.

Tests cover:
- _coerce_intent / _coerce_risk default-on-fail behavior
- classify_facts mutates fact attributes from canned predictor output
- filter_safety_blocked drops safety-class facts
- classifier failure path keeps schema defaults (extraction never blocks)

Real LLM is not used here — a stub predictor returns canned `dspy.Prediction`
objects so we can assert the mutation logic without network calls.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import dspy
import pytest

from memex_core.memory.extraction.classifier import (
    DEFAULT_INTENT,
    DEFAULT_RISK,
    INTENT_VALUES,
    RISK_VALUES,
    classify_facts,
    filter_safety_blocked,
    _coerce_intent,
    _coerce_risk,
)
from memex_core.memory.extraction.models import ProcessedFact


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


class TestClassifyFacts:
    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self) -> None:
        result = await classify_facts([], lm=object(), predictor=object())  # type: ignore[arg-type]
        assert result == []

    @pytest.mark.asyncio
    async def test_mutates_facts_with_predictor_output(self) -> None:
        fact = _make_fact()
        canned = dspy.Prediction(intent_class='ephemeral', risk_class='sensitive')

        async def fake_run_dspy(*_args: object, **_kwargs: object) -> dspy.Prediction:
            return canned

        with patch('memex_core.memory.extraction.classifier.run_dspy_operation', fake_run_dspy):
            await classify_facts([fact], lm=object(), predictor=object())  # type: ignore[arg-type]

        assert fact.intent_class == 'ephemeral'
        assert fact.risk_class == 'sensitive'

    @pytest.mark.asyncio
    async def test_keeps_defaults_when_predictor_raises(self) -> None:
        fact = _make_fact()
        original_intent = fact.intent_class
        original_risk = fact.risk_class

        async def boom(*_args: object, **_kwargs: object) -> dspy.Prediction:
            raise RuntimeError('LLM provider unreachable')

        with patch('memex_core.memory.extraction.classifier.run_dspy_operation', boom):
            await classify_facts([fact], lm=object(), predictor=object())  # type: ignore[arg-type]

        assert fact.intent_class == original_intent == DEFAULT_INTENT
        assert fact.risk_class == original_risk == DEFAULT_RISK

    @pytest.mark.asyncio
    async def test_invalid_predictor_output_falls_back_to_defaults(self) -> None:
        fact = _make_fact()
        bogus = dspy.Prediction(intent_class='unknown', risk_class='nope')

        async def fake_run_dspy(*_args: object, **_kwargs: object) -> dspy.Prediction:
            return bogus

        with patch('memex_core.memory.extraction.classifier.run_dspy_operation', fake_run_dspy):
            await classify_facts([fact], lm=object(), predictor=object())  # type: ignore[arg-type]

        assert fact.intent_class == DEFAULT_INTENT
        assert fact.risk_class == DEFAULT_RISK

    @pytest.mark.asyncio
    async def test_classifies_each_fact_independently(self) -> None:
        facts = [_make_fact(text=f'fact {i}') for i in range(3)]
        outputs = [
            dspy.Prediction(intent_class='permanent', risk_class='none'),
            dspy.Prediction(intent_class='durable', risk_class='sensitive'),
            dspy.Prediction(intent_class='ephemeral', risk_class='private'),
        ]
        call_idx = {'i': 0}

        async def fake_run_dspy(*_args: object, **_kwargs: object) -> dspy.Prediction:
            out = outputs[call_idx['i']]
            call_idx['i'] += 1
            return out

        with patch('memex_core.memory.extraction.classifier.run_dspy_operation', fake_run_dspy):
            await classify_facts(facts, lm=object(), predictor=object())  # type: ignore[arg-type]

        assert [f.intent_class for f in facts] == ['permanent', 'durable', 'ephemeral']
        assert [f.risk_class for f in facts] == ['none', 'sensitive', 'private']


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
