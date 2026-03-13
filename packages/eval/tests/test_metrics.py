"""Tests for metrics models — scoring, aggregation, and serialization."""

from __future__ import annotations

import datetime as dt

from memex_eval.metrics import (
    BenchmarkResult,
    CheckResult,
    CheckStatus,
    GroupResult,
)


# ---------------------------------------------------------------------------
# CheckStatus
# ---------------------------------------------------------------------------


class TestCheckStatus:
    def test_values(self):
        assert CheckStatus.PASS.value == 'pass'
        assert CheckStatus.FAIL.value == 'fail'
        assert CheckStatus.SKIP.value == 'skip'
        assert CheckStatus.ERROR.value == 'error'


# ---------------------------------------------------------------------------
# GroupResult
# ---------------------------------------------------------------------------


def _make_group(
    statuses: list[CheckStatus],
    name: str = 'g',
) -> GroupResult:
    g = GroupResult(name=name, description='test')
    for i, status in enumerate(statuses):
        g.checks.append(
            CheckResult(
                name=f'c{i}',
                group=name,
                status=status,
                description='',
                query='q',
                expected='e',
            )
        )
    return g


class TestGroupResult:
    def test_all_pass(self):
        g = _make_group([CheckStatus.PASS, CheckStatus.PASS])
        assert g.passed == 2
        assert g.failed == 0
        assert g.total == 2
        assert g.pass_rate == 1.0

    def test_mixed(self):
        g = _make_group([CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.SKIP])
        assert g.passed == 1
        assert g.failed == 1
        assert g.skipped == 1
        assert g.total == 3
        assert g.pass_rate == 0.5  # 1 passed / 2 runnable

    def test_all_skipped(self):
        g = _make_group([CheckStatus.SKIP, CheckStatus.SKIP])
        assert g.pass_rate == 0.0

    def test_error_count(self):
        g = _make_group([CheckStatus.ERROR, CheckStatus.PASS])
        assert g.errored == 1
        assert g.passed == 1

    def test_empty(self):
        g = GroupResult(name='empty', description='empty')
        assert g.total == 0
        assert g.pass_rate == 0.0


# ---------------------------------------------------------------------------
# BenchmarkResult
# ---------------------------------------------------------------------------


class TestBenchmarkResult:
    def test_aggregate_totals(self):
        r = BenchmarkResult()
        r.groups = [
            _make_group([CheckStatus.PASS, CheckStatus.FAIL]),
            _make_group([CheckStatus.PASS, CheckStatus.PASS]),
        ]
        assert r.total_passed == 3
        assert r.total_failed == 1
        assert r.total_checks == 4
        assert r.overall_pass_rate == 0.75

    def test_duration_ms(self):
        r = BenchmarkResult()
        r.started_at = dt.datetime(2025, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        r.finished_at = dt.datetime(2025, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc)
        assert r.duration_ms == 5000.0

    def test_duration_ms_unfinished(self):
        r = BenchmarkResult()
        r.finished_at = None
        assert r.duration_ms == 0.0

    def test_to_dict_structure(self):
        r = BenchmarkResult(vault_name='test-vault')
        r.groups = [_make_group([CheckStatus.PASS], name='g1')]
        r.finished_at = dt.datetime.now(dt.timezone.utc)

        d = r.to_dict()
        assert 'started_at' in d
        assert 'finished_at' in d
        assert 'summary' in d
        assert d['summary']['total'] == 1
        assert d['summary']['passed'] == 1
        assert d['vault_name'] == 'test-vault'
        assert len(d['groups']) == 1
        assert d['groups'][0]['name'] == 'g1'
        assert len(d['groups'][0]['checks']) == 1

    def test_empty_result(self):
        r = BenchmarkResult()
        assert r.total_checks == 0
        assert r.overall_pass_rate == 0.0
