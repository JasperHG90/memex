"""Tests for report generation — terminal output and JSON export."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from memex_eval.metrics import BenchmarkResult, CheckResult, CheckStatus, GroupResult
from memex_eval.report import export_json, print_report


def _sample_result() -> BenchmarkResult:
    """Build a small but complete BenchmarkResult for report tests."""
    g = GroupResult(name='basic', description='Basic tests')
    g.checks.append(
        CheckResult(
            name='keyword_check',
            group='basic',
            status=CheckStatus.PASS,
            description='Check keywords',
            query='project alpha',
            expected='alpha',
            actual='Found all: alpha',
            duration_ms=150.5,
        )
    )
    g.checks.append(
        CheckResult(
            name='missing_check',
            group='basic',
            status=CheckStatus.FAIL,
            description='Check missing keyword',
            query='project gamma',
            expected='gamma',
            actual='Missing: gamma',
            duration_ms=200.0,
        )
    )
    g.ingest_duration_ms = 5000.0

    r = BenchmarkResult(vault_name='test-vault')
    r.groups.append(g)
    r.finished_at = dt.datetime.now(dt.timezone.utc)
    return r


class TestPrintReport:
    def test_print_report_does_not_raise(self):
        """Smoke test: print_report should complete without error."""
        result = _sample_result()
        print_report(result)

    def test_print_report_empty(self):
        """Smoke test: empty result should also work."""
        print_report(BenchmarkResult())


class TestExportJson:
    def test_export_creates_file(self, tmp_path: Path):
        result = _sample_result()
        out_file = tmp_path / 'results.json'
        export_json(result, out_file)

        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data['summary']['total'] == 2
        assert data['summary']['passed'] == 1
        assert data['summary']['failed'] == 1

    def test_export_json_roundtrip(self, tmp_path: Path):
        result = _sample_result()
        out_file = tmp_path / 'results.json'
        export_json(result, out_file)

        data = json.loads(out_file.read_text())
        assert len(data['groups']) == 1
        checks = data['groups'][0]['checks']
        assert checks[0]['status'] == 'pass'
        assert checks[1]['status'] == 'fail'

    def test_export_with_string_path(self, tmp_path: Path):
        result = _sample_result()
        out_file = str(tmp_path / 'results.json')
        export_json(result, out_file)
        assert Path(out_file).exists()
