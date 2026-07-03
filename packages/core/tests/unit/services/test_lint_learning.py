"""Unit tests for the lint auto-learning loop's classification logic.

The `classify_verdict` function is the load-bearing bit of Layer 2: every
counter on the telemetry rollup is decided by it. These tests pin it
against synthetic row dicts so the rules cannot drift without breaking
the test, and so the contract is testable without a Postgres roundtrip.
"""

from __future__ import annotations

from datetime import datetime, timezone

from memex_core.services.lint_learning import (
    LintRuleTelemetryDTO,
    _Bucket,
    classify_verdict,
)


class TestClassifyVerdict:
    def test_resolved_with_canned_action_is_accept(self) -> None:
        row = {
            'status': 'resolved',
            'evidence': {
                'resolution': {
                    'followup': {'action': 'deprioritize_unit', 'params': {}},
                },
            },
        }
        assert classify_verdict(row) == 'accept'

    def test_resolved_with_no_op_action_is_no_op(self) -> None:
        row = {
            'status': 'resolved',
            'evidence': {
                'resolution': {
                    'followup': {'action': 'no_op'},
                },
            },
        }
        assert classify_verdict(row) == 'no_op'

    def test_resolved_without_resolution_block_is_legacy(self) -> None:
        # Pre-cockpit row: status was flipped but no resolution payload exists.
        row = {'status': 'resolved', 'evidence': {'rule_specific': 'fields'}}
        assert classify_verdict(row) == 'legacy'

    def test_resolved_with_resolution_but_no_followup_is_legacy(self) -> None:
        # E.g. a pure dismiss-with-note flow on a non-action verdict path.
        row = {
            'status': 'resolved',
            'evidence': {'resolution': {'verdict': 'accepted', 'actor': 'x'}},
        }
        assert classify_verdict(row) == 'legacy'

    def test_resolved_with_followup_missing_action_is_legacy(self) -> None:
        row = {
            'status': 'resolved',
            'evidence': {'resolution': {'followup': {}}},
        }
        assert classify_verdict(row) == 'legacy'

    def test_resolved_with_followup_null_action_is_legacy(self) -> None:
        row = {
            'status': 'resolved',
            'evidence': {'resolution': {'followup': {'action': None}}},
        }
        assert classify_verdict(row) == 'legacy'

    def test_dismissed_is_dismiss_regardless_of_evidence(self) -> None:
        assert classify_verdict({'status': 'dismissed', 'evidence': {}}) == 'dismiss'
        assert (
            classify_verdict(
                {
                    'status': 'dismissed',
                    'evidence': {'resolution': {'followup': {'action': 'deprioritize_unit'}}},
                }
            )
            == 'dismiss'
        )

    def test_unknown_status_treated_as_legacy(self) -> None:
        # Pending rows shouldn't reach the rollup at all (the SQL excludes
        # them); defensive default for any malformed state value.
        assert classify_verdict({'status': 'pending', 'evidence': {}}) == 'legacy'

    def test_evidence_as_string_is_legacy(self) -> None:
        # asyncpg should always decode jsonb to dict, but defend against
        # any future driver / serialiser change.
        assert classify_verdict({'status': 'resolved', 'evidence': '{}'}) == 'legacy'

    def test_archive_mental_model_counts_as_accept(self) -> None:
        row = {
            'status': 'resolved',
            'evidence': {
                'resolution': {
                    'followup': {'action': 'archive_mental_model'},
                },
            },
        }
        assert classify_verdict(row) == 'accept'


class TestBucketAggregation:
    def test_observe_increments_correct_counter(self) -> None:
        bucket = _Bucket()
        bucket.observe('accept', 0.8, 120)
        bucket.observe('accept', 0.9, 60)
        bucket.observe('no_op', 0.5, None)
        bucket.observe('dismiss', None, 30)
        bucket.observe('legacy', None, None)
        assert bucket.accept == 2
        assert bucket.no_op == 1
        assert bucket.dismiss == 1
        assert bucket.legacy == 1

    def test_upsert_params_carries_medians(self) -> None:
        bucket = _Bucket()
        for surprise, ttr in [(0.5, 10), (0.7, 20), (0.9, 30)]:
            bucket.observe('accept', surprise, ttr)
        params = bucket.upsert_params(
            rule_name='r',
            vault_id=None,
            window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        # Median of [0.5, 0.7, 0.9] = 0.7; median of [10, 20, 30] = 20.
        assert params['median_surprise'] == 0.7
        assert params['median_time_to_resolve_seconds'] == 20
        assert params['accept_count'] == 3
        assert params['no_op_count'] == 0

    def test_upsert_params_handles_empty_observations(self) -> None:
        bucket = _Bucket()
        params = bucket.upsert_params(
            rule_name='r',
            vault_id='v',
            window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        assert params['median_surprise'] is None
        assert params['median_time_to_resolve_seconds'] is None
        assert params['accept_count'] == 0

    def test_negative_ttr_is_ignored(self) -> None:
        # A row with resolved_at earlier than created_at (clock skew) shouldn't
        # poison the median.
        bucket = _Bucket()
        bucket.observe('accept', None, -1)
        bucket.observe('accept', None, 30)
        params = bucket.upsert_params(
            rule_name='r',
            vault_id=None,
            window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        assert params['median_time_to_resolve_seconds'] == 30


class TestTelemetryDTODerivedFields:
    def _dto(self, **overrides: object) -> LintRuleTelemetryDTO:
        base: dict[str, object] = {
            'rule_name': 'r',
            'vault_id': None,
            'window_start': datetime(2026, 1, 1, tzinfo=timezone.utc),
            'window_end': datetime(2026, 2, 1, tzinfo=timezone.utc),
            'accept_count': 0,
            'no_op_count': 0,
            'dismiss_count': 0,
            'legacy_count': 0,
            'median_surprise': None,
            'median_time_to_resolve_seconds': None,
            'refreshed_at': datetime(2026, 2, 1, tzinfo=timezone.utc),
        }
        base.update(overrides)
        return LintRuleTelemetryDTO(**base)  # type: ignore[arg-type]

    def test_accept_rate_excludes_legacy(self) -> None:
        dto = self._dto(accept_count=7, no_op_count=2, dismiss_count=1, legacy_count=50)
        # (7 accepts + 2 no_op) / (7+2+1) = 0.9. Legacy must not dilute.
        # no_op counts as positive engagement (operator reviewed and acted).
        assert dto.accept_rate is not None
        assert abs(dto.accept_rate - 0.9) < 1e-9

    def test_accept_rate_none_when_no_labelled(self) -> None:
        dto = self._dto(accept_count=0, no_op_count=0, dismiss_count=0, legacy_count=20)
        assert dto.accept_rate is None

    def test_total_count_includes_legacy(self) -> None:
        dto = self._dto(accept_count=1, no_op_count=2, dismiss_count=3, legacy_count=4)
        assert dto.total_count == 10
        assert dto.labelled_count == 6
