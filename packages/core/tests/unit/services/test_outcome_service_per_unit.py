"""Unit tests for the per-unit `UnitOutcome` schema + legacy-shim translation.

These tests run against the resolver layer (`OutcomeService._resolve_unit_outcomes`)
and the `UnitOutcome` Pydantic model directly. The DB-touching path is covered
by the integration suite — here we pin the schema contract, the verb dispatch
math, and the legacy `(unit_ids, success)` translation including the
`FutureWarning` deprecation signal.
"""

from __future__ import annotations

import warnings
from uuid import uuid4

import pytest

from memex_core.services.outcomes import OutcomeService, UnitOutcome


# ---------------------------------------------------------------------------
# UnitOutcome Pydantic validator
# ---------------------------------------------------------------------------


class TestUnitOutcomeValidator:
    def test_helpful_requires_reason(self):
        with pytest.raises(ValueError, match='reason'):
            UnitOutcome(unit_id=uuid4(), verb='helpful', reason=None)

    def test_helpful_rejects_blank_reason(self):
        with pytest.raises(ValueError, match='reason'):
            UnitOutcome(unit_id=uuid4(), verb='helpful', reason='   ')

    def test_not_helpful_requires_reason(self):
        with pytest.raises(ValueError, match='reason'):
            UnitOutcome(unit_id=uuid4(), verb='not_helpful', reason=None)

    def test_not_used_accepts_no_reason(self):
        uo = UnitOutcome(unit_id=uuid4(), verb='not_used')
        assert uo.reason is None

    def test_not_used_accepts_reason(self):
        uo = UnitOutcome(unit_id=uuid4(), verb='not_used', reason='retrieved but irrelevant')
        assert uo.reason == 'retrieved but irrelevant'

    def test_invalid_verb_rejected(self):
        with pytest.raises(ValueError):
            UnitOutcome(unit_id=uuid4(), verb='maybe', reason='x')  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Resolver: input normalisation + legacy shim
# ---------------------------------------------------------------------------


class TestResolveUnitOutcomes:
    def test_units_passthrough_objects(self):
        u1 = UnitOutcome(unit_id=uuid4(), verb='helpful', reason='r1')
        u2 = UnitOutcome(unit_id=uuid4(), verb='not_used')
        out = OutcomeService._resolve_unit_outcomes(
            units=[u1, u2], unit_ids=None, success=None, reason=None
        )
        assert out == [u1, u2]

    def test_units_passthrough_dicts(self):
        uid = uuid4()
        out = OutcomeService._resolve_unit_outcomes(
            units=[{'unit_id': str(uid), 'verb': 'helpful', 'reason': 'r'}],
            unit_ids=None,
            success=None,
            reason=None,
        )
        assert len(out) == 1
        assert out[0].verb == 'helpful'
        assert out[0].unit_id == uid

    def test_legacy_success_true_translates_to_helpful(self):
        uid = uuid4()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            out = OutcomeService._resolve_unit_outcomes(
                units=None,
                unit_ids=[str(uid)],
                success=True,
                reason=None,
            )
        assert any(issubclass(x.category, FutureWarning) for x in w)
        assert len(out) == 1
        assert out[0].verb == 'helpful'
        assert out[0].unit_id == uid
        assert out[0].reason  # legacy supplies a default reason

    def test_legacy_success_false_translates_to_not_helpful(self):
        uid = uuid4()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            out = OutcomeService._resolve_unit_outcomes(
                units=None,
                unit_ids=[str(uid)],
                success=False,
                reason=None,
            )
        assert any(issubclass(x.category, FutureWarning) for x in w)
        assert out[0].verb == 'not_helpful'

    def test_legacy_reason_passes_through(self):
        uid = uuid4()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            out = OutcomeService._resolve_unit_outcomes(
                units=None,
                unit_ids=[str(uid)],
                success=True,
                reason='custom-reason',
            )
        assert out[0].reason == 'custom-reason'

    def test_legacy_invalid_uuids_dropped(self):
        uid = uuid4()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            out = OutcomeService._resolve_unit_outcomes(
                units=None,
                unit_ids=[str(uid), 'not-a-uuid'],
                success=True,
                reason='r',
            )
        assert len(out) == 1
        assert out[0].unit_id == uid

    def test_units_and_unit_ids_simultaneously_rejected(self):
        with pytest.raises(ValueError, match='not both'):
            OutcomeService._resolve_unit_outcomes(
                units=[UnitOutcome(unit_id=uuid4(), verb='not_used')],
                unit_ids=['u1'],
                success=True,
                reason=None,
            )

    def test_unit_ids_without_success_rejected(self):
        with pytest.raises(ValueError, match='success'):
            OutcomeService._resolve_unit_outcomes(
                units=None,
                unit_ids=['u1'],
                success=None,
                reason=None,
            )

    def test_empty_returns_empty(self):
        out = OutcomeService._resolve_unit_outcomes(
            units=None, unit_ids=None, success=None, reason=None
        )
        assert out == []

    def test_outcome_rejects_units_and_success_together(self):
        with pytest.raises(ValueError, match='Cannot mix'):
            OutcomeService._resolve_unit_outcomes(
                units=[UnitOutcome(unit_id=uuid4(), verb='not_used')],
                unit_ids=None,
                success=True,
                reason=None,
            )


class TestUnitOutcomeReasonLengthCap:
    def test_reason_under_cap_accepted(self):
        uo = UnitOutcome(unit_id=uuid4(), verb='helpful', reason='a' * 200)
        assert uo.reason == 'a' * 200

    def test_reason_over_cap_rejected(self):
        with pytest.raises(ValueError):
            UnitOutcome(unit_id=uuid4(), verb='helpful', reason='a' * 201)
