"""Unit tests for ``RetrievalRequest`` intent_class / risk_class validation.

These tests instantiate ``RetrievalRequest`` directly and assert that the
post-init model validator rejects values outside the canonical enum sets.
They do not touch the database or the retrieval engine, so they belong in
the unit suite (``pytest -m 'not integration'``); previously they lived
inside an ``@pytest.mark.integration``-marked class and were silently
skipped by unit-only CI runs.
"""

import pytest

from memex_core.memory.retrieval.models import RetrievalRequest


class TestIntentRiskValidation:
    def test_invalid_intent_class_rejected(self):
        with pytest.raises(ValueError, match='Invalid intent_class'):
            RetrievalRequest(query='x', intent_class='not-a-real-class')

    def test_invalid_risk_class_rejected(self):
        with pytest.raises(ValueError, match='Invalid risk_class'):
            RetrievalRequest(query='x', risk_class='not-a-real-class')
