"""Unit tests for the F25 / F25b write-time classifier surface.

After F25b the standalone classifier predictor is gone — intent + risk arrive
on each fact directly from the extraction LLM. What remains is:

- ``filter_safety_blocked`` — drops ``risk_class='safety'`` facts before
  persistence.
- The per-fact pydantic validators on ``RawFact`` that enforce default-on-fail
  coercion at parse time. (The module-level ``_coerce_intent`` / ``_coerce_risk``
  helpers were retired in the F25b follow-up — they had become dead production
  code duplicating the validators. Default-on-fail behavior is now exercised
  through ``RawFact`` directly in ``TestRawFactValidators``.)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_common.schemas import IntentClass, RiskClass
from memex_core.memory.extraction.classifier import filter_safety_blocked
from memex_core.memory.extraction.models import ProcessedFact, RawFact

# Single source of truth: derive valid values + defaults directly from the
# canonical enums (was previously re-exported from classifier.py until
# Hermes round-5 retired the orphaned constants).
INTENT_VALUES: tuple[str, ...] = tuple(c.value for c in IntentClass)
RISK_VALUES: tuple[str, ...] = tuple(c.value for c in RiskClass)
DEFAULT_INTENT = IntentClass.DURABLE.value
DEFAULT_RISK = RiskClass.NONE.value


def _make_fact(text: str = 'a fact', context: str = '') -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type='world',
        embedding=[0.0] * 384,
        mentioned_at='2026-01-01T00:00:00+00:00',
        context=context,
        vault_id=uuid4(),
    )


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

    @pytest.mark.parametrize(
        'garbage', ['', 'unknown', None, 42, ['durable'], {'intent': 'durable'}]
    )
    def test_intent_non_string_garbage_falls_back_to_default(self, garbage: object) -> None:
        # Coverage previously held by ``TestCoerce`` against ``_coerce_intent``;
        # after the helper was retired, RawFact's validator must absorb the
        # same non-string garbage shapes (None, int, list, dict).
        rf = RawFact(what='x', fact_type='world', intent_class=garbage, risk_class='none')  # type: ignore[arg-type]
        assert rf.intent_class == DEFAULT_INTENT

    @pytest.mark.parametrize('garbage', ['', 'unknown', None, 42, ['none']])
    def test_risk_non_string_garbage_falls_back_to_default(self, garbage: object) -> None:
        rf = RawFact(what='x', fact_type='world', intent_class='durable', risk_class=garbage)  # type: ignore[arg-type]
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
